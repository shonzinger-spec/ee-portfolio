#!/usr/bin/env python3
"""Plot the Wokwi on-target run: docs/wokwi_serial.log (firmware) + docs/wokwi_run.log (chip).

Top: VLOAD, bottom: current, against firmware control time (20 us per tick; the Wokwi build
runs the loop 20x slower in simulated time, see README_WOKWI.md).
  firmware  100 us means of the ADC readings (VSENSE, ISENSE = load current), 10 ms apart
            outside the capture windows
  plant     the chip's own PLANT rows (VLOAD, inductor current IL), in plant time shifted onto
            control time; the shift is taken from the 0.5 -> 3 A step (chip PSEG t0 vs the
            firmware STEP t_us), so the overlay is good to the firmware's 100 us resolution.
Events: START, READY, load changes (chip), STEP, FAULT.

usage: python3 sim/wokwi/plot_wokwi.py [serial_log] [run_log] [out_png]   (from 02-sync-buck/)
"""
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

root = Path(__file__).resolve().parents[2]
serial = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "docs/wokwi_serial.log"
runlog = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "docs/wokwi_run.log"
out = Path(sys.argv[3]) if len(sys.argv) > 3 else root / "docs/img/wokwi_buck.png"

kv = lambda line: dict(re.findall(r"(\w+)=(-?[\d.]+)", line))

rows, ev = [], {"START": [], "READY": [], "STEP": [], "FAULT": []}
for line in serial.read_text(errors="replace").splitlines():
    if re.match(r"^\d+,[-\d.]+,[-\d.]+,", line):
        rows.append([float(x) for x in line.split(",")])
    else:
        for k in ev:
            if line.startswith(k + " "):
                ev[k].append(kv(line))
fw = np.array(rows)
if fw.size == 0:
    sys.exit(f"no trace rows in {serial}")

plant, segs = [], []
for line in runlog.read_text(errors="replace").splitlines():
    if "PLANT tp_us=" in line:
        d = kv(line)
        plant.append([float(d["tp_us"]), float(d["vload_V"]), float(d["il_A"])])
    elif "PSEG " in line:
        segs.append(kv(line))
pl = np.array(plant) if plant else np.zeros((0, 3))

# shift plant time onto control time with the 0.5 -> 3 A step
shift = None
up = [s for s in segs if int(s["load_mA"]) == 500 and int(s["next_load_mA"]) == 3000]
st = [e for e in ev["STEP"] if float(e["i_to_A"]) > 2.0 and float(e["i_from_A"]) < 2.0]
if up and st:
    shift = float(up[0]["t1_us"]) - float(st[0]["t_us"])

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True)
ms = lambda us: np.asarray(us, dtype=float) * 1e-3
ax1.plot(ms(fw[:, 0]), fw[:, 1], ".", ms=3, color="tab:blue", label="firmware VSENSE (100 us mean)")
ax2.plot(ms(fw[:, 0]), fw[:, 2], ".", ms=3, color="tab:blue", label="firmware ISENSE = load current")
if shift is not None and len(pl):
    p2 = pl[pl[:, 0] - shift >= 0].copy()
    p2[:, 0] -= shift
    # the chip prints every 100 us for 8 ms after a control change, else every 2 ms:
    # draw lines only between 100 us rows, dots for the sparse ones
    gap = np.diff(p2[:, 0]) > 300
    br = np.insert(p2, np.where(gap)[0] + 1, np.nan, axis=0)
    for ax, col, lab in ((ax1, 1, "plant VLOAD (chip)"), (ax2, 2, "plant inductor current IL (chip)")):
        ax.plot(ms(br[:, 0]), br[:, col], "-", lw=0.8, color="tab:orange", label=lab)
        ax.plot(ms(p2[:, 0]), p2[:, col], ".", ms=2, color="tab:orange")
    for s in segs[1:]:
        x = (float(s["t0_us"]) - shift) * 1e-3
        for ax in (ax1, ax2):
            ax.axvline(x, color="0.6", lw=0.8, ls=":")
        ax2.annotate(f"load {int(s['load_mA']) / 1000:g} A", (x, 0.02), xycoords=("data", "axes fraction"),
                     rotation=90, fontsize=7, color="0.4", ha="right", va="bottom")
for name, color in (("START", "tab:green"), ("READY", "tab:green"), ("FAULT", "tab:red")):
    for e in ev[name]:
        x = float(e["t_us"]) * 1e-3
        for ax in (ax1, ax2):
            ax.axvline(x, color=color, lw=1.0, ls="--")
        ax1.annotate(name, (x, 0.97), xycoords=("data", "axes fraction"), rotation=90,
                     fontsize=8, color=color, ha="right", va="top")
for e in ev["STEP"]:
    x = float(e["t_us"]) * 1e-3
    ax1.annotate(f"STEP {float(e['i_from_A']):.1f}->{float(e['i_to_A']):.1f} A\n"
                 f"min {float(e['vmin_V']):.3f} max {float(e['vmax_V']):.3f} V",
                 (x, 0.05), xycoords=("data", "axes fraction"), fontsize=7, color="tab:purple")
ax1.axhspan(4.95, 5.05, color="tab:green", alpha=0.08, lw=0, label="5 V +/-1 %")
ax1.set_ylabel("V")
ax2.set_ylabel("A")
ax2.set_xlabel("control time (ms), 20 us per update")
ax1.set_title("02 sync buck on a simulated ESP32 (Wokwi, LEDC build) against the plant.c custom chip")
for ax in (ax1, ax2):
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, loc="center right")
ax1.set_ylim(-0.2, 6.0)
fig.tight_layout()
out.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(out, dpi=130)
print(f"wrote {out} ({len(fw)} firmware rows, {len(pl)} plant rows, plant shift "
      f"{'n/a' if shift is None else f'{shift:.0f} us'})")
