#!/usr/bin/env python3
"""Smart Power-Monitoring Node (project 01): standalone model of the design.

Reproduces the shunt front-end and the INA219 digital readout end to end without
SPICE, a simulator or hardware. Needs Python 3.9+ and numpy; matplotlib is used
only for the figure.

    python3 main.py             # self-check table + figure docs/img/main_py_frontend.png
    python3 main.py --no-plot   # self-check table only (matplotlib not needed)

Exit code 0 when every check passes, 1 when any check fails.

What is modelled
  Analog  : the load-current profile of sim/shunt_frontend.cir (PWL, 30 ms, 1 us
            grid), Vsh = I*R, Vload = 5 V - Vsh, Psh = I^2*R, and the 10 ohm +
            10 ohm / 100 nF differential input filter as a first-order RC.
  Digital : the INA219 register chain per TI SBOS448 (SHUNT, BUS, CURRENT, POWER)
            and the values the firmware (src/power_node/Ina219.h) derives from it.

What is not modelled
  The digital model is instantaneous, like the Wokwi chip model
  (sim/wokwi/ina219.chip.c). The real part averages 64 conversions (about 68 ms
  per shunt + bus pair) before a reading is ready, so it would smear the 30 ms
  profile below. The 2 us input filter is far faster than any ADC conversion, so
  the digital path reads Vsh directly. Noise, I2C timing and PCB layout are out
  of scope.

Author: Shon Zinger. See README.md, section Credits, for the AI assistance note.
"""
import argparse
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
FIGURE_PATH = HERE / "docs" / "img" / "main_py_frontend.png"

# --------------------------------------------------------------------------
# Constants mirrored from src/power_node/config.h (keep in sync with it)
# --------------------------------------------------------------------------
R_SHUNT_OHM = 0.05                 # SHUNT_OHMS
V_RAIL = 5.0                       # monitored supply rail
INA219_PGA = 0b10                  # PGA /4: +/-160 mV
CURRENT_LSB_A = 0.0001             # CURRENT_LSB_A, 100 uA per bit
# Datasheet: Cal = trunc(0.04096 / (Current_LSB * R_shunt)). config.h adds 0.5
# before truncating to guard against floating-point round-off; so does this.
CAL = int(0.04096 / (CURRENT_LSB_A * R_SHUNT_OHM) + 0.5)          # 8192
CURRENT_LSB_MA = CURRENT_LSB_A * 1000.0                           # 0.1 mA
POWER_LSB_MW = 20.0 * CURRENT_LSB_MA                              # 2 mW, fixed 20x by silicon
SHUNT_FS_RAW = 4000 * (1 << INA219_PGA)                           # 16000 = 160.00 mV
SHUNT_SAT_RAW = SHUNT_FS_RAW - 10                                 # 15990 = 159.90 mV -> saturated
SAMPLE_PERIOD_MS = 100             # SAMPLE_PERIOD_MS: firmware sensor period

# INA219 register scaling (TI SBOS448)
SHUNT_LSB_V = 10e-6                # 10 uV per SHUNT count
BUS_LSB_V = 4e-3                   # 4 mV per BUS count
BUS_MAX = 0x1FFF                   # 13-bit bus field
CURRENT_DIV = 4096                 # CURRENT = SHUNT * CAL / 4096
POWER_DIV = 5000                   # POWER   = CURRENT * BUS / 5000

# --------------------------------------------------------------------------
# Constants from sim/shunt_frontend.cir (not in config.h)
# --------------------------------------------------------------------------
RF_OHM = 10.0                      # Rf1 = Rf2
CF_F = 100e-9                      # Cf, differential
TAU_S = 2 * RF_OHM * CF_F          # 2 us (the 50 mOhm shunt adds 0.25 %, ignored)
T_END_S = 30e-3
DT_S = 1e-6
# PWL points (s, A): 100 us linear ramps between plateaus
PWL = [(0.0, 0.0), (2e-3, 0.0), (2.1e-3, 0.5), (6e-3, 0.5), (6.1e-3, 1.5),
       (10e-3, 1.5), (10.1e-3, 2.0), (14e-3, 2.0), (14.1e-3, 0.2),
       (18e-3, 0.2), (18.1e-3, 0.0), (30e-3, 0.0)]

# Figure: markers every PLOT_MARKER_MS. The firmware samples every 100 ms, which
# puts a single sample (t = 0) inside the 30 ms profile, so the figure uses a
# finer spacing to show how the readout follows the load.
PLOT_MARKER_MS = 1

# --------------------------------------------------------------------------
# Expected results
# --------------------------------------------------------------------------
REL_TOL = 1e-3                     # 0.1 %
# (name, expected, unit) from the "expect" comments in sim/shunt_frontend.cir
ANALOG_EXPECT = [
    ("Vsh_max", 0.100, "V"),
    ("Vina_max", 0.100, "V"),
    ("Vload_min", 4.90, "V"),
    ("Psh_pk", 0.200, "W"),
    ("Isense_pk", 2.0, "A"),
    ("Vsh_at_1p5 (9 ms)", 0.075, "V"),
]
# Live Wokwi run, 2026-10-27 (docs/wokwi_serial.log, docs/wokwi_serial_steps.log).
# Serial CSV fields: bus_V,shunt_mV,current_mA,power_mW. None = read() false.
WOKWI_EXPECT = [
    (1000, "4.952,50.00,1000.0,4952.0"),
    (2000, "4.900,100.00,2000.0,9800.0"),
    (3200, None),                  # OLED shows SENSOR ERR, no CSV row
]
# Saturation boundary implied by SHUNT_SAT_RAW = 15990 (159.90 mV = 3198 mA at 50 mOhm)
BOUNDARY_EXPECT = [(3197, True), (3198, False)]


# --------------------------------------------------------------------------
# Analog model
# --------------------------------------------------------------------------
def load_current(t):
    """Load current in A: the netlist's PWL source."""
    tp, ip = zip(*PWL)
    return np.interp(t, tp, ip)


def analog_transient():
    """30 ms on a 1 us grid: returns dict of numpy arrays."""
    n = int(round(T_END_S / DT_S)) + 1
    t = np.arange(n) * DT_S
    i_load = load_current(t)
    vsh = i_load * R_SHUNT_OHM
    vload = V_RAIL - vsh
    psh = i_load ** 2 * R_SHUNT_OHM
    # V(inp,inn): first-order RC, exact step response per 1 us step (tau = 2 us)
    alpha = 1.0 - np.exp(-DT_S / TAU_S)
    vina = np.empty(n)
    vina[0] = vsh[0]
    for k in range(1, n):
        vina[k] = vina[k - 1] + alpha * (vsh[k] - vina[k - 1])
    return {"t": t, "i": i_load, "vsh": vsh, "vload": vload, "psh": psh, "vina": vina}


# --------------------------------------------------------------------------
# INA219 digital model (integer arithmetic, as the chip does it)
# --------------------------------------------------------------------------
def _div_round(num, den):
    """Integer divide, round half away from zero (as ina219.chip.c div_round)."""
    num = np.asarray(num, dtype=np.int64)
    return np.sign(num) * ((np.abs(num) + den // 2) // den)


def _div_trunc(num, den):
    """Integer divide truncating toward zero (C semantics, not Python floor)."""
    num = np.asarray(num, dtype=np.int64)
    return np.sign(num) * (np.abs(num) // den)


def ina219_registers(vsh_v, vload_v):
    """SHUNT, BUS, CURRENT, POWER raw register values and the OVF flag.

    Inputs are snapped to the nearest nanovolt first. That removes float noise
    from exact half-LSB ties (4.95 V / 4 mV = 1237.5 must round up to 1238)
    without moving any real value by more than 0.5 nV. A plain float
    floor(x + 0.5) disagrees with the chip's integer arithmetic for 2 of the
    3201 integer-mA loads (3000 mA and 3160 mA, both BUS ties).
    """
    vsh_nv = np.rint(np.asarray(vsh_v, dtype=float) * 1e9).astype(np.int64)
    vld_nv = np.rint(np.asarray(vload_v, dtype=float) * 1e9).astype(np.int64)
    shunt = np.clip(_div_round(vsh_nv, 10_000), -SHUNT_FS_RAW, SHUNT_FS_RAW)
    bus = np.clip(_div_round(np.maximum(vld_nv, 0), 4_000_000), 0, BUS_MAX)
    cur = _div_trunc(shunt * CAL, CURRENT_DIV)
    ovf = (cur > 32767) | (cur < -32768)
    cur = np.clip(cur, -32768, 32767)
    pwr = _div_trunc(np.abs(cur) * bus, POWER_DIV)
    ovf = ovf | (pwr > 65535)
    pwr = np.clip(pwr, 0, 65535)
    return {"shunt": shunt, "bus": bus, "current": cur, "power": pwr, "ovf": ovf}


def firmware_reading(regs):
    """What Ina219::read() returns for scalar registers: dict or None (invalid).

    Float32 arithmetic, like the ESP32 firmware (0.004f, 0.01f, CURRENT_LSB_MA,
    POWER_LSB_MW are all float).
    """
    shunt, bus = int(regs["shunt"]), int(regs["bus"])
    cur, pwr = int(regs["current"]), int(regs["power"])
    if bool(regs["ovf"]) or abs(shunt) >= SHUNT_SAT_RAW:
        return None
    f32 = np.float32
    return {
        "bus_V": f32(bus) * f32(0.004),
        "shunt_mV": f32(shunt) * f32(0.01),
        "current_mA": f32(cur) * f32(CURRENT_LSB_MA),
        "power_mW": f32(pwr) * f32(POWER_LSB_MW),
    }


def csv_fields(reading):
    """Format as the sketch's Serial.printf("%.3f,%.2f,%.1f,%.1f")."""
    return "%.3f,%.2f,%.1f,%.1f" % (float(reading["bus_V"]), float(reading["shunt_mV"]),
                                    float(reading["current_mA"]), float(reading["power_mW"]))


def model_for_load_ma(i_ma):
    """Registers and firmware reading for a constant load in mA (the Wokwi slider)."""
    vsh = (i_ma / 1000.0) * R_SHUNT_OHM
    regs = ina219_registers(vsh, V_RAIL - vsh)
    return regs, firmware_reading(regs)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def analog_checks(d):
    t, vsh = d["t"], d["vsh"]
    got = {
        "Vsh_max": vsh.max(),
        "Vina_max": d["vina"].max(),
        "Vload_min": d["vload"].min(),
        "Psh_pk": (vsh * d["i"]).max(),
        "Isense_pk": (vsh / R_SHUNT_OHM).max(),
        "Vsh_at_1p5 (9 ms)": vsh[int(np.argmin(np.abs(t - 9e-3)))],
    }
    rows = []
    for name, exp, unit in ANALOG_EXPECT:
        g = float(got[name])
        rel = abs(g - exp) / abs(exp)
        rows.append((name, exp, g, unit, rel, rel <= REL_TOL))
    return rows


def digital_checks():
    rows = []
    for i_ma, exp in WOKWI_EXPECT:
        regs, rd = model_for_load_ma(i_ma)
        got = csv_fields(rd) if rd is not None else None
        rows.append(("%d mA" % i_ma, exp, got, regs, got == exp))
    return rows


def boundary_checks():
    rows = []
    for i_ma, exp_valid in BOUNDARY_EXPECT:
        regs, rd = model_for_load_ma(i_ma)
        rows.append((i_ma, int(regs["shunt"]), exp_valid, rd is not None))
    return rows


def sample_readout(d):
    """INA219 CURRENT reading (A) every PLOT_MARKER_MS, NaN where invalid."""
    step = int(round(PLOT_MARKER_MS * 1e-3 / DT_S))
    idx = np.arange(0, len(d["t"]), step)
    regs = ina219_registers(d["vsh"][idx], d["vload"][idx])
    valid = ~regs["ovf"] & (np.abs(regs["shunt"]) < SHUNT_SAT_RAW)
    cur_a = np.where(valid, regs["current"] * CURRENT_LSB_A, np.nan)
    return d["t"][idx], cur_a


# --------------------------------------------------------------------------
# Figure
# --------------------------------------------------------------------------
def make_figure(d, t_s, reading_a, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping the figure (pip install matplotlib)")
        return False

    surface, ink, ink2, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e1e0d9"
    c_load, c_read, c_vsh, c_vina = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6"
    t_ms = d["t"] * 1e3

    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(9, 6.4), dpi=150,
                                   facecolor=surface)
    for ax in (ax1, ax2):
        ax.set_facecolor(surface)
        ax.grid(True, color=grid, lw=0.8)
        ax.tick_params(colors=ink2, labelsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c3c2b7")
        ax.yaxis.label.set_color(ink)

    ax1.plot(t_ms, d["i"], color=c_load, lw=1.6, label="Load current (PWL source)")
    ax1.plot(t_s * 1e3, reading_a, ls="none", marker="o", ms=5.5, mfc=c_read,
             mec=surface, mew=1.0,
             label="INA219 CURRENT reading, every %d ms" % PLOT_MARKER_MS)
    ax1.set_ylabel("Current (A)", fontsize=10)
    ax1.set_ylim(-0.1, 2.35)
    ax1.legend(loc="upper right", fontsize=8.5, frameon=False, labelcolor=ink)
    ax1.set_title("Project 01: shunt front-end and INA219 readout (main.py)",
                  fontsize=11, color=ink, loc="left")

    ax2.plot(t_ms, d["vsh"] * 1e3, color=c_vsh, lw=1.8, label="Vsh = I x R")
    ax2.plot(t_ms, d["vina"] * 1e3, color=c_vina, lw=1.3, ls=(0, (4, 2)),
             label="V(inp,inn) after 10 + 10 ohm / 100 nF (tau 2 us)")
    ax2.set_ylabel("Voltage (mV)", fontsize=10)
    ax2.set_xlabel("Time (ms)", fontsize=10, color=ink)
    ax2.set_ylim(-5, 150)
    ax2.legend(loc="upper right", fontsize=8.5, frameon=False, labelcolor=ink)
    ax2.annotate("100 mV at 2 A", xy=(12, 100), xytext=(14.6, 108), fontsize=8.5,
                 color=ink2, arrowprops=dict(arrowstyle="-", color="#898781", lw=0.8))

    # Inset (empty region after 18 ms): the 2 us filter lag on the 2 A ramp is invisible at full scale.
    ins = ax2.inset_axes([0.64, 0.24, 0.32, 0.40])
    ins.set_facecolor(surface)
    win = (t_ms >= 10.04) & (t_ms <= 10.14)
    ins.plot(t_ms[win], d["vsh"][win] * 1e3, color=c_vsh, lw=1.8)
    ins.plot(t_ms[win], d["vina"][win] * 1e3, color=c_vina, lw=1.3, ls=(0, (4, 2)))
    ins.set_title("10.04-10.14 ms", fontsize=7.5, color=ink2, pad=2)
    ins.tick_params(colors=ink2, labelsize=7)
    ins.grid(True, color=grid, lw=0.6)
    for spine in ins.spines.values():
        spine.set_color("#c3c2b7")

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=surface)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-plot", action="store_true", help="skip the figure")
    args = ap.parse_args(argv)

    print("Smart Power Node 01 - main.py")
    print("Constants (mirrored from src/power_node/config.h)")
    print("  R_shunt %.2f ohm | rail %.0f V | PGA /4 (+/-%d mV) | Current_LSB %.0f uA"
          % (R_SHUNT_OHM, V_RAIL, SHUNT_FS_RAW // 100, CURRENT_LSB_A * 1e6))
    print("  CAL %d | Power_LSB %.0f mW | saturation threshold %d counts (%.1f mV)"
          % (CAL, POWER_LSB_MW, SHUNT_SAT_RAW, SHUNT_SAT_RAW * SHUNT_LSB_V * 1e3))
    failures = 0
    for name, got, exp in (("CAL", CAL, 8192), ("saturation threshold", SHUNT_SAT_RAW, 15990),
                           ("Power_LSB mW", round(POWER_LSB_MW, 9), 2.0)):
        ok = got == exp
        failures += not ok
        print("  %-22s %-8g expected %-8g %s" % (name, got, exp, "PASS" if ok else "FAIL"))

    d = analog_transient()
    print("\nAnalog transient: %d points, %.0f us grid, 0 to %.0f ms, filter tau %.1f us"
          % (len(d["t"]), DT_S * 1e6, T_END_S * 1e3, TAU_S * 1e6))
    print("%-18s %10s %12s %9s  %s" % ("check", "expected", "computed", "rel err", "result"))
    for name, exp, got, unit, rel, ok in analog_checks(d):
        failures += not ok
        print("%-18s %10s %12s %8.4f%%  %s" % (name, "%g %s" % (exp, unit),
              "%.7g %s" % (got, unit), rel * 100, "PASS" if ok else "FAIL"))

    print("\nINA219 digital model vs live Wokwi run (exact string match)")
    print("CSV fields: bus_V,shunt_mV,current_mA,power_mW")
    print("%-8s %-27s %-27s %s" % ("load", "Wokwi", "model", "result"))
    for label, exp, got, regs, ok in digital_checks():
        failures += not ok
        print("%-8s %-27s %-27s %s" % (label, exp or "invalid (SENSOR ERR)",
              got or "invalid (SENSOR ERR)", "PASS" if ok else "FAIL"))
        print("         registers: SHUNT=%d BUS=%d CURRENT=%d POWER=%d"
              % (regs["shunt"], regs["bus"], regs["current"], regs["power"]))

    print("\nSaturation boundary (threshold %d counts)" % SHUNT_SAT_RAW)
    for i_ma, shunt_raw, exp_valid, got_valid in boundary_checks():
        ok = exp_valid == got_valid
        failures += not ok
        print("%4d mA  SHUNT=%d  %s  %s" % (i_ma, shunt_raw,
              "valid  " if got_valid else "invalid", "PASS" if ok else "FAIL"))
    max_cur = SHUNT_FS_RAW * CAL // CURRENT_DIV
    ovf_ok = max_cur <= 32767
    failures += not ovf_ok
    print("OVF unreachable: max CURRENT raw at full scale = %d <= 32767  %s"
          % (max_cur, "PASS" if ovf_ok else "FAIL"))

    n_fw = int(T_END_S * 1e3) // SAMPLE_PERIOD_MS + 1
    print("\nFirmware samples every %d ms: %d sample falls inside the 30 ms profile (t = 0)."
          % (SAMPLE_PERIOD_MS, n_fw))
    if not args.no_plot:
        t_s, reading_a = sample_readout(d)
        if make_figure(d, t_s, reading_a, FIGURE_PATH):
            print("Figure written: %s (INA219 markers every %d ms)"
                  % (FIGURE_PATH.relative_to(HERE), PLOT_MARKER_MS))

    print("\nRESULT: %s" % ("ALL PASS" if failures == 0 else "%d FAIL" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
