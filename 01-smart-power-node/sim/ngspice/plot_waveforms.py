#!/usr/bin/env python3
"""Run the ngspice port + LTspice (batch, scratch copy), compare, plot.

Usage: python3 sim/ngspice/plot_waveforms.py      (from anywhere)
Needs ngspice on PATH (or set NGSPICE=/path/to/ngspice). LTspice is optional: set
LTSPICE=/path/to/LTspice, or leave the macOS default; without it the LTspice-vs-ngspice
residual and the second figure are skipped and the first figure is still written.
Writes: docs/sim_ngspice.log, docs/img/waveform_shunt.png,
        docs/img/waveform_ltspice_vs_ngspice.png, sim/ngspice/ngspice_wave.txt
"""
import datetime, os, pathlib, re, shutil, subprocess, sys, tempfile
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NGSPICE = os.environ.get("NGSPICE") or shutil.which("ngspice")
LTSPICE = os.environ.get("LTSPICE", "/Applications/LTspice.app/Contents/MacOS/LTspice")
LOG = ROOT / "docs/sim_ngspice.log"
IMG = ROOT / "docs/img"

# LTspice reference results (docs/sim_ltspice.log): name -> (value, abs tol)
LT_REF = {"Vsh_max": 0.0999999, "Vina_max": 0.0999999, "Vload_min": 4.9,
          "Psh_pk": 0.2, "Isense_pk": 2.0, "Vsh_at_1p5": 0.0749998}
ABS_TOL = 1e-6  # 1 uV / 1 uA for near-zero values
# LTspice prints 7 digits, so its own rounding is ~1e-7 relative; 0.1 % is far above that.


def run_ngspice():
    if not NGSPICE:
        sys.exit("ngspice not found: install it or set NGSPICE=/path/to/ngspice")
    r = subprocess.run([NGSPICE, "-b", "shunt_frontend_ngspice.cir"], cwd=HERE,
                       capture_output=True, text=True, check=False)
    out = r.stdout + r.stderr
    hdr = (f"# ngspice batch run: cd sim/ngspice && ngspice -b shunt_frontend_ngspice.cir  "
           f"({datetime.datetime.now().strftime('%a %b %d %H:%M:%S %Y')})\n")
    LOG.write_text(hdr + out)
    meas = {}
    for line in out.splitlines():  # 'name = value' lines from `print`
        m = re.match(r"^(\w+) = (\S+)$", line.strip())
        if m:
            meas[m.group(1).lower()] = float(m.group(2))
    return meas


def run_ltspice():
    """Run LTspice in a temporary copy of the netlist; None when LTspice is not installed."""
    if not pathlib.Path(LTSPICE).exists():
        print(f"LTspice not found at {LTSPICE}: skipping the LTspice comparison")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        scratch = pathlib.Path(tmp)
        shutil.copy(ROOT / "sim/shunt_frontend.cir", scratch / "shunt_frontend.cir")
        subprocess.run([LTSPICE, "-ascii", "-b", "shunt_frontend.cir"], cwd=scratch,
                       capture_output=True, check=False)
        return parse_raw(scratch / "shunt_frontend.raw")


def parse_raw(path):
    b = path.read_bytes()
    enc = "utf-16le"
    vals = b.find("Values:\n".encode(enc))
    binr = b.find("Binary:\n".encode(enc))
    split = vals if vals >= 0 else binr
    head = b[:split].decode(enc)
    names = re.findall(r"^\t\d+\t(\S+)\t", head, re.M)
    npts = int(re.search(r"No. Points:\s+(\d+)", head).group(1))
    nv = len(names)
    if vals >= 0:  # ASCII: "idx\tv0\n\tv1\n..."
        body = b[vals + len("Values:\n".encode(enc)):].decode(enc).split()
        arr = np.array(body, dtype=float).reshape(npts, nv + 1)[:, 1:]
    else:  # binary: float64 time, float32 others
        raw = b[binr + len("Binary:\n".encode(enc)):]
        dt = np.dtype([("t", "<f8")] + [(f"v{i}", "<f4") for i in range(nv - 1)])
        rec = np.frombuffer(raw, dtype=dt, count=npts)
        arr = np.column_stack([rec["t"]] + [rec[f"v{i}"] for i in range(nv - 1)])
    d = {n: arr[:, i] for i, n in enumerate(names)}
    d["time"] = np.abs(d["time"])  # compressed points are stored negative
    assert np.all(np.diff(d["time"]) >= 0), "time not monotonic"
    assert abs(d["V(sup)"].mean() - 5) < 1e-3, "V(sup) not ~5 V"
    return d


def main():
    meas = run_ngspice()
    lt = run_ltspice()
    w = np.loadtxt(HERE / "ngspice_wave.txt")  # t vsh t vina t ish
    t_ng, vsh_ng, vina_ng, ish_ng = w[:, 0], w[:, 1], w[:, 3], w[:, 5]
    if lt is not None:
        t_lt = lt["time"]
        vsh_lt = lt["V(sup)"] - lt["V(load)"]

    # ---- comparison table
    rows, ok_all = [], True
    for k, ref in LT_REF.items():
        ng = meas[k.lower()]
        diff = ng - ref
        rel = abs(diff) / abs(ref) if ref else 0.0
        ok = rel <= 1e-3 or abs(diff) <= ABS_TOL
        ok_all &= ok
        rows.append(f"{k:<12}{ref:>14.7g}{ng:>14.7g}{diff:>13.3e}{rel*100:>10.4f}  {'PASS' if ok else 'FAIL'}")
    tg = np.linspace(0, 30e-3, 30001)  # common grid, 1 us
    if lt is not None:
        ng_i = np.interp(tg, t_ng, vsh_ng)
        lt_i = np.interp(tg, t_lt, vsh_lt)
        res_uV = (lt_i - ng_i) * 1e6
    table = ["", "# Comparison ngspice vs LTspice (PASS: rel <= 0.1 % or abs <= 1e-6)",
             f"{'measurement':<12}{'LTspice':>14}{'ngspice':>14}{'diff':>13}{'rel %':>10}  result"] + rows
    table.append(f"RESULT: {sum(r.endswith('PASS') for r in rows)}/{len(rows)} PASS")
    if lt is not None:
        table.append(f"max |residual| V(sup,load) LTspice-ngspice on 1 us grid: {np.abs(res_uV).max():.3f} uV "
                     f"at t = {tg[np.abs(res_uV).argmax()]*1e3:.4f} ms")
    else:
        table.append("LTspice not run: residual not computed (LTspice column is the stored reference)")
    with LOG.open("a") as f:
        f.write("\n".join(table) + "\n")
    print("\n".join(table))

    # ---- plot 1: ngspice only
    plt.rcParams.update({"font.size": 11, "axes.grid": True, "grid.alpha": 0.3})
    IMG.mkdir(parents=True, exist_ok=True)
    fig, (a1, a2) = plt.subplots(2, 1, sharex=True, figsize=(10, 6.5))
    tm = t_ng * 1e3
    a1.plot(tm, ish_ng, color="tab:blue", lw=1.6)
    a1.set_ylabel("Load current I (A)")
    a1.set_title("Shunt front-end, ngspice transient (Rshunt = 50 mohm, 5 V rail)")
    steps = [(2.1, 0.5, 4.0), (6.1, 1.5, 8.0), (10.1, 2.0, 12.0), (14.1, 0.2, 16.0)]
    for x, a, xt in steps:
        a1.annotate(f"{a:g} A", xy=(x, a), xytext=(xt, a + 0.25),
                    arrowprops=dict(arrowstyle="->", lw=0.8), ha="center")
    a1.set_ylim(-0.1, 2.5)
    a2.plot(tm, vsh_ng * 1e3, color="tab:red", lw=1.6, label="V(sup,load) shunt")
    a2.plot(tm, vina_ng * 1e3, color="k", lw=1.0, ls="--", label="V(inp,inn) after 10 ohm + 100 nF filter")
    for x, a, xt in steps:
        a2.annotate(f"{a*50:g} mV", xy=(x + 1.0, a * 50), xytext=(xt + 0.5, a * 50 + 12),
                    arrowprops=dict(arrowstyle="->", lw=0.8), ha="center")
    a2.set_ylabel("Voltage (mV)")
    a2.set_xlabel("Time (ms)")
    a2.set_ylim(-10, 125)
    a2.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(IMG / "waveform_shunt.png", dpi=150)
    plt.close(fig)

    # ---- plot 2: LTspice vs ngspice
    if lt is None:
        print("LTspice not run: waveform_ltspice_vs_ngspice.png not rewritten")
        return
    fig, (b1, b2) = plt.subplots(2, 1, sharex=True, figsize=(10, 6.5),
                                 gridspec_kw={"height_ratios": [3, 1]})
    b1.plot(t_lt * 1e3, vsh_lt * 1e3, color="tab:orange", lw=3.5, alpha=0.7, label="LTspice")
    b1.plot(t_ng * 1e3, vsh_ng * 1e3, color="k", lw=1.0, ls="--", label="ngspice")
    b1.set_ylabel("V(sup,load) (mV)")
    b1.set_title("Shunt voltage: LTspice vs ngspice")
    b1.legend(loc="upper right")
    b2.plot(tg * 1e3, res_uV, color="tab:purple", lw=1.0)
    b2.set_ylabel("Residual (uV)\nLTspice - ngspice")
    b2.set_xlabel("Time (ms)")
    fig.tight_layout()
    fig.savefig(IMG / "waveform_ltspice_vs_ngspice.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
