#!/usr/bin/env python3
"""BLDC 6-step controller (project 05): standalone averaged model and cross-checks.

A 24 V, 4-pole-pair BLDC motor with trapezoidal back-EMF, driven by a 3-phase MOSFET inverter
with Hall-sensor 6-step commutation, cascaded speed (1 kHz) and current (20 kHz) PI loops and
500 ns firmware dead time. Needs Python 3.9+ and numpy; matplotlib is used only for the figure.

    python3 main.py             # self-check table + docs/img/main_py_bldc.png
    python3 main.py --no-plot   # self-check table only (matplotlib not needed)

Exit code 0 when every check passes, 1 when any check fails.

Three models of the same drive, each checked against the next:
  main.py   : PWM-period-averaged inverter (dead time and switch delays as average voltage
              terms, body diodes on the floating phase), the same motor, and a Python port of
              the controller in src/bldc/bldc.c. Fast, so it can sweep and plot.
  host test : test/host/test_bldc.c runs the C firmware against a switch-level plant stepped
              every timer tick (31.25 ns): its METRIC lines are compared with this model.
  ngspice   : sim/ngspice/*.cir, MOSFET models with gate drive: dead-time sweep of one leg, and
              the full inverter at a fixed operating point compared with the host test's S0.
Constants are read from src/bldc/bldc_config.h and test/host/plant.h.
"""
import argparse
import csv
import math
import pathlib
import re
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_H = HERE / "src" / "bldc" / "bldc_config.h"
PLANT_H = HERE / "test" / "host" / "plant.h"
HOST_LOG = HERE / "docs" / "host_test.log"
NEG_LOG = HERE / "docs" / "host_negative.log"
SPICE_LOG = HERE / "docs" / "sim_ngspice.log"
LEG_DECK = HERE / "sim" / "ngspice" / "leg_deadtime.cir"
SCHEMATIC = HERE / "hw" / "kicad" / "bldc.kicad_sch"
ERC_LOG = HERE / "docs" / "kicad_erc.log"
HIL_TRACE = HERE / "docs" / "hil_trace.csv"
PWM_TRACE = HERE / "docs" / "pwm_trace.csv"
FIGURE_PATH = HERE / "docs" / "img" / "main_py_bldc.png"


def read_defines(path, prefix):
    """#define NAME value pairs whose value is a plain number (C suffixes and parentheses ok)."""
    out = {}
    for m in re.finditer(r"#define[ \t]+(%s\w+)[ \t]+([^\n]+)" % prefix, path.read_text()):
        v = re.sub(r"/\*.*", "", m.group(2)).strip().strip("()")
        v = re.sub(r"(?<=[\d.])[fFlL]$", "", v)
        try:
            out[m.group(1)] = float(v)
        except ValueError:
            continue
    return out


CFG = read_defines(CONFIG_H, "BLDC_")
PL = read_defines(PLANT_H, "PL_")
ARR = int(CFG["BLDC_TIM_ARR"])
F_PWM = CFG["BLDC_TIM_CLK_HZ"] / (2 * ARR)
T_PWM = 1.0 / F_PWM
TICK = 1.0 / CFG["BLDC_TIM_CLK_HZ"]
DT_TICKS = int((CFG["BLDC_DT_NS"] * (CFG["BLDC_TIM_CLK_HZ"] / 1e6) + 999) // 1000)
PP = int(CFG["BLDC_POLE_PAIRS"])
RPM = 60.0 / (2 * math.pi)            # rad/s -> rpm

RESULTS = []


def check(name, ok, detail):
    RESULTS.append(bool(ok))
    print("  %-46s %-46s %s" % (name, detail, "PASS" if ok else "FAIL"))


def trap(deg):
    """Trapezoidal back-EMF shape, +/-1 with 120 deg flat tops (same as plant_trap)."""
    a = deg % 360.0
    if a < 30.0:
        return a / 30.0
    if a <= 150.0:
        return 1.0
    if a < 210.0:
        return 1.0 - (a - 150.0) / 30.0
    if a <= 330.0:
        return -1.0
    return -1.0 + (a - 330.0) / 30.0


HALL_ERR = (PL["PL_HALL_ERR_A"], PL["PL_HALL_ERR_B"], PL["PL_HALL_ERR_C"])
def c_table(name):
    """An int array initialiser from src/bldc/bldc.c, so the port uses the firmware's tables."""
    m = re.search(r"%s\[\d+\]\s*=\s*\{([^}]*)\}" % name, (HERE / "src" / "bldc" / "bldc.c").read_text())
    return tuple(int(v) for v in m.group(1).split(","))


HALL_TO_SECTOR = c_table("hall_to_sector")
SECTOR_HIGH = c_table("bldc_sector_high")
SECTOR_LOW = c_table("bldc_sector_low")


def hall_code(el_deg):
    code = 0
    for x in range(3):
        if (el_deg + 30.0 - 120.0 * x - HALL_ERR[x]) % 360.0 < 180.0:
            code |= 1 << x
    return code


# --------------------------------------------------------------------------
# Averaged inverter + motor
# --------------------------------------------------------------------------
def leg_fractions(duty):
    """Per PWM period, for the PWM leg: fraction with the high switch conducting, and fraction
    with both switches off (body diode) - from the firmware's compare pair and the plant's
    switch delays."""
    c = ARR - int(duty * ARR + 0.5)
    ch, cl = c + DT_TICKS // 2, c - (DT_TICKS - DT_TICKS // 2)
    if ch > ARR:
        on = 0.0
    else:
        on = max(0.0, 2.0 * (ARR - ch) - PL["PL_TD_ON"] + PL["PL_TD_OFF"]) / (2 * ARR)
    gap = 0.0
    if cl < ARR:
        gap = (DT_TICKS - (PL["PL_TD_OFF"] - PL["PL_TD_ON"])) / ARR
        if ch > ARR:
            gap = 2.0 * (ARR - cl) / (2 * ARR)
    return on, max(gap, 0.0)


class Drive:
    """Averaged 3-phase inverter, star motor and rotor; the controller sets regs each period."""

    def __init__(self, hold_w=None):
        self.i = [0.0, 0.0, 0.0]
        self.w = 0.0 if hold_w is None else hold_w
        self.hold = hold_w is not None
        self.theta = 0.0
        self.t = 0.0
        self.t_load = 0.0
        self.mode = [None, None, None]   # 'pwm', 'low', None (floating) per leg, from regs
        self.duty = 0.0
        self.i_src = 0.0
        self.e_in = 0.0
        self.e_mech = 0.0

    def el(self):
        return math.degrees(self.theta) * PP

    def step(self, dt):
        vb, rds, vf = PL["PL_VBUS"], PL["PL_RDS_ON"], PL["PL_VF"]
        r, l, ke = PL["PL_R_PH"], PL["PL_L_PH"], PL["PL_KE_PH"]
        el = self.el()
        e = [ke * self.w * trap(el - 120.0 * x) for x in range(3)]
        on, gap = leg_fractions(self.duty)
        v, conn = [0.0] * 3, [False] * 3
        for x in range(3):
            ix = self.i[x]
            if self.mode[x] == "pwm":
                vd = -vf if ix >= 0 else vb + vf
                v[x] = on * vb + gap * vd - rds * ix
                conn[x] = True
            elif self.mode[x] == "low":
                v[x] = -rds * ix
                conn[x] = True
            elif ix > 1e-9:
                v[x], conn[x] = -vf, True
            elif ix < -1e-9:
                v[x], conn[x] = vb + vf, True
        k = sum(conn)
        vn = sum(v[x] - e[x] - r * self.i[x] for x in range(3) if conn[x]) / k if k >= 2 else vb / 2
        if k >= 2:
            for x in range(3):
                if not conn[x] and (e[x] + vn > vb + vf or e[x] + vn < -vf):
                    v[x], conn[x] = (vb + vf if e[x] + vn > vb else -vf), True
            k = sum(conn)
            vn = sum(v[x] - e[x] - r * self.i[x] for x in range(3) if conn[x]) / k
        if k < 2:
            self.i = [0.0, 0.0, 0.0]
        else:
            clamped = [False] * 3
            for x in range(3):
                if not conn[x]:
                    continue
                inew = self.i[x] + (v[x] - e[x] - r * self.i[x] - vn) / l * dt
                diode = self.mode[x] is None
                if diode and self.i[x] * inew <= 0.0:
                    inew, clamped[x] = 0.0, True
                self.i[x] = inew
            live = [x for x in range(3) if conn[x] and not clamped[x]]
            res = sum(self.i)
            for x in live:
                self.i[x] -= res / len(live)
        torque = sum(ke * trap(el - 120.0 * x) * self.i[x] for x in range(3))
        # supply current: the PWM leg's high switch while on, and any upper body diode
        isrc = 0.0
        for x in range(3):
            if self.mode[x] == "pwm":
                isrc += on * self.i[x] + (gap * self.i[x] if self.i[x] < 0 else 0.0)
            elif self.mode[x] is None and self.i[x] < 0:
                isrc += self.i[x]
        self.i_src = isrc
        self.e_in += vb * isrc * dt
        self.e_mech += torque * self.w * dt
        if not self.hold:
            tc = PL["PL_TC"] + self.t_load              # reactive load, as in plant.c
            net = torque - PL["PL_B"] * self.w
            if abs(self.w) < 1e-9 and abs(net) <= tc:
                self.w = 0.0
            else:
                d = 1.0 if self.w > 1e-9 else (-1.0 if self.w < -1e-9 else math.copysign(1.0, net))
                wn = self.w + (net - tc * d) / PL["PL_J"] * dt
                self.w = 0.0 if self.w != 0.0 and wn * self.w < 0.0 else wn
        self.theta += self.w * dt
        self.t += dt


class Controller:
    """Python port of src/bldc/bldc.c: speed measurement, speed and current PI, stop, commutation.
    The fault checks (over-current, Hall, stall) are not ported; only the C host test covers them."""

    def __init__(self):
        self.rpm_cmd = self.rpm_ref = self.rpm = 0.0
        self.i_ref = self.i_meas = self.duty = 0.0
        self.spd_int = self.cur_int = 0.0
        self.duty_out = self.duty_prev = 0.0
        self.periods, self.edge_last, self.have_edge = [], 0.0, False
        self.speed_div = 0
        self.running = False

    def edge(self, t):
        if self.have_edge:
            self.periods = ([t - self.edge_last] + self.periods)[:6]
        self.have_edge = True
        self.edge_last = t

    def isr(self, now, sample, sector, vbus):
        c = CFG
        if self.duty_prev >= c["BLDC_DUTY_MIN_SAMPLE"]:
            self.i_meas = sample
        self.duty_prev, self.duty_out = self.duty_out, 0.0
        since = now - self.edge_last
        if not self.periods or since > c["BLDC_SPEED_TIMEOUT_MS"] / 1000.0:
            self.rpm = 0.0
        else:
            acc, k, longest = 0.0, 0, 0.0
            for per in self.periods:
                if k > 0 and acc + per > c["BLDC_SPEED_WINDOW_US"] * 1e-6:
                    break
                acc, k, longest = acc + per, k + 1, max(longest, per)
            tp = acc / k
            if since > longest and since > tp:
                tp = since
            self.rpm = 60.0 / (tp * 6 * PP)
        if not self.running:
            if self.rpm_cmd <= 0:
                return None
            self.running = True
            self.spd_int = self.cur_int = self.rpm_ref = 0.0
        self.speed_div += 1
        if self.speed_div >= c["BLDC_SPEED_DIV"]:
            self.speed_div = 0
            dt_s = c["BLDC_SPEED_DIV"] * T_PWM
            step = c["BLDC_RAMP_RPM_S"] * dt_s
            self.rpm_ref = min(self.rpm_ref + step, self.rpm_cmd) if self.rpm_ref < self.rpm_cmd \
                else max(self.rpm_ref - step, self.rpm_cmd)
            err = self.rpm_ref - self.rpm
            self.spd_int = min(max(self.spd_int + c["BLDC_SPD_KI"] * err * dt_s, 0.0), c["BLDC_I_LIMIT_A"])
            self.i_ref = min(max(c["BLDC_SPD_KP"] * err + self.spd_int, 0.0), c["BLDC_I_LIMIT_A"])
            if self.rpm_cmd <= 0 and self.rpm_ref <= 0:
                self.running = False
                self.i_ref = self.duty = 0.0
                return None
        err = self.i_ref - self.i_meas
        ff = c["BLDC_KE_LL"] * self.rpm / RPM / vbus if vbus > 1.0 else 0.0
        dmax, dmin = c["BLDC_DUTY_MAX"], c["BLDC_DUTY_MIN_SAMPLE"]
        u = ff + c["BLDC_CUR_KP"] * err + self.cur_int
        if not ((u >= dmax and err > 0) or (u <= dmin and err < 0)):
            self.cur_int = min(max(self.cur_int + c["BLDC_CUR_KI"] * err * T_PWM, -dmax), dmax)
        self.duty = min(max(ff + c["BLDC_CUR_KP"] * err + self.cur_int, c["BLDC_DUTY_MIN_SAMPLE"]), dmax)
        if sector < 0:
            return None
        self.duty_out = self.duty
        return sector, self.duty


def regs_for(sector, duty):
    s = (sector + int(CFG["BLDC_COMM_ADVANCE"])) % 6
    mode = [None, None, None]
    mode[SECTOR_HIGH[s]] = "pwm"
    mode[SECTOR_LOW[s]] = "low"
    return mode, duty


def simulate(events, t_end, hold_w=None, open_loop_duty=None, sub=6):
    """Run the averaged drive. events: list of (t, 'cmd'|'load', value). Returns sampled arrays
    (one row per PWM period) and the drive. The pipeline matches the hardware: registers written
    in ISR k take effect at update k+1; the current sample is taken mid-period."""
    d = Drive(hold_w)
    ctl = Controller()
    ev = sorted(events)
    pending = None
    sample = 0.0
    out = {k: [] for k in ("t", "rpm", "rpm_ref", "i_ref", "i_meas", "isrc", "ia", "ib", "ic", "duty")}
    hall_prev = hall_code(d.el())
    n = int(round(t_end / T_PWM))
    for k in range(n):
        now = k * T_PWM
        while ev and ev[0][0] <= now + 1e-12:
            _, kind, val = ev.pop(0)
            if kind == "cmd":
                ctl.rpm_cmd = val
            else:
                d.t_load = val
        if pending is not None:                         # preload transfer at the update event
            d.mode, d.duty = pending
        elif k > 0:
            d.mode, d.duty = [None, None, None], 0.0
        sector = HALL_TO_SECTOR[hall_code(d.el())]
        if open_loop_duty is not None:
            pending = regs_for(sector, open_loop_duty)
        else:
            r = ctl.isr(now, sample, sector, PL["PL_VBUS"])
            pending = regs_for(*r) if r else None
        isrc_acc = 0.0
        for j in range(sub):
            d.step(T_PWM / sub)
            isrc_acc += d.i_src
            if j == sub // 2 - 1:
                # after half the period (sub is even): the DC-link shunt at the counter peak, the
                # PWM leg's current while its high side is on
                pw = [x for x in range(3) if d.mode[x] == "pwm"]
                sample = d.i[pw[0]] if pw and leg_fractions(d.duty)[0] > 0 else 0.0
            h = hall_code(d.el())
            if h != hall_prev:
                if HALL_TO_SECTOR[h] >= 0 and HALL_TO_SECTOR[hall_prev] >= 0:
                    ctl.edge(d.t)
                hall_prev = h
        out["t"].append(d.t)
        out["rpm"].append(d.w * RPM)
        out["rpm_ref"].append(ctl.rpm_ref)
        out["i_ref"].append(ctl.i_ref)
        out["i_meas"].append(ctl.i_meas)
        out["isrc"].append(isrc_acc / sub)
        out["ia"].append(d.i[0])
        out["ib"].append(d.i[1])
        out["ic"].append(d.i[2])
        out["duty"].append(ctl.duty if open_loop_duty is None else open_loop_duty)
    return {k: np.array(v) for k, v in out.items()}, d


# --------------------------------------------------------------------------
# Log readers
# --------------------------------------------------------------------------
def host_metrics(txt):
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"^METRIC (\w+) (\S+)", txt, re.M)}


def spice_values(txt):
    sweep = {}
    for m in re.finditer(r"^SWEEP (\S+) (\S+) (\S+) (\S+)", txt, re.M):
        sweep[m.group(1)] = tuple(float(v) for v in m.group(2, 3, 4))
    delay = {}
    for m in re.finditer(r"^DELAY (.*)$", txt, re.M):
        f = m.group(1).split()
        for a, b in zip(f[0::2], f[1::2]):
            delay[a] = float(b)
    six = re.search(r"^SIXSTEP (\S+) (\S+) (\S+)", txt, re.M)
    bemf = re.search(r"^BEMF (\S+) (\S+)", txt, re.M)
    return sweep, delay, tuple(float(v) for v in six.groups()), tuple(float(v) for v in bemf.groups())


def si(s):
    """'500n' -> 5e-07"""
    mult = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
    return float(s[:-1]) * mult[s[-1]] if s[-1] in mult else float(s)


def window(res, key, t0, t1):
    m = (res["t"] > t0) & (res["t"] <= t1)
    return res[key][m]


def last_outside(res, t0, t1, target, band=0.01):
    m = (res["t"] > t0) & (res["t"] <= t1)
    t, r = res["t"][m], res["rpm"][m]
    out = t[np.abs(r - target) > band * target]
    return out[-1] if len(out) else t0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-plot", action="store_true", help="skip the figure")
    args = ap.parse_args()

    print("BLDC 05 - main.py (simulated design; no hardware built)")
    print("Firmware %s: PWM %.0f Hz, dead time %d ticks = %.0f ns, current limit %.1f A, OC trip %.0f A"
          % (CONFIG_H.relative_to(HERE), F_PWM, DT_TICKS, DT_TICKS * TICK * 1e9,
             CFG["BLDC_I_LIMIT_A"], CFG["BLDC_OC_TRIP_A"]))
    print()

    # [1] constants
    print("[1] Constants")
    check("C01 headers parsed", len(CFG) >= 20 and len(PL) >= 15,
          "%d firmware + %d plant constants" % (len(CFG), len(PL)))
    check("C02 PWM 20 kHz, dead time 500 ns in ticks", F_PWM == 20000 and DT_TICKS * TICK >= 500e-9 - 1e-12,
          "%.0f Hz, %d x %.2f ns" % (F_PWM, DT_TICKS, TICK * 1e9))
    check("C03 firmware Ke and poles = plant",
          abs(CFG["BLDC_KE_LL"] - 2 * PL["PL_KE_PH"]) < 1e-9 and PP == PL["PL_POLE_PAIRS"],
          "Ke_LL %.3f = 2 x %.4f, %d pole pairs" % (CFG["BLDC_KE_LL"], PL["PL_KE_PH"], PP))
    shunt_gain = PL["PL_RSHUNT"] * 20 * 1000          # INA181A1 gain 20, mV per A
    check("C04 current-sense scale = shunt x INA181A1 gain",
          abs(shunt_gain - CFG["BLDC_ISENSE_MV_PER_A"]) < 1e-9,
          "%.4f ohm x 20 = %.0f mV/A, range +/-%.0f A" % (
              PL["PL_RSHUNT"], shunt_gain, 1650.0 / shunt_gain))
    print()

    # [2] averaged model against closed forms
    print("[2] Averaged model (Python) against closed forms")
    on, gap = leg_fractions(CFG["BLDC_DUTY_MAX"])
    rloop = 2 * PL["PL_R_PH"] + 2 * PL["PL_RDS_ON"]
    kt = 2 * PL["PL_KE_PH"]
    veff = PL["PL_VBUS"] * on - PL["PL_VF"] * gap
    w_cf = (veff - rloop * PL["PL_TC"] / kt) / (kt + rloop * PL["PL_B"] / kt)
    res_nl, _ = simulate([], 0.6, open_loop_duty=CFG["BLDC_DUTY_MAX"])
    w_sim = window(res_nl, "rpm", 0.5, 0.6).mean() / RPM
    check("C05 no-load speed at max duty vs closed form", abs(w_sim / w_cf - 1) < 0.02,
          "model %.0f, flat-top formula %.0f rpm" % (w_sim * RPM, w_cf * RPM))
    i_lr = (PL["PL_VBUS"] * leg_fractions(0.2)[0] - PL["PL_VF"] * leg_fractions(0.2)[1]) / rloop
    tau = 2 * PL["PL_L_PH"] / rloop
    res_lr, _ = simulate([], 5 * tau, hold_w=0.0, open_loop_duty=0.2)
    i_tau = np.interp(tau, res_lr["t"], np.maximum(np.abs(res_lr["ia"]), np.abs(res_lr["ic"])))
    i_end = max(abs(res_lr["ia"][-1]), abs(res_lr["ic"][-1]))
    check("C06 locked rotor, duty 0.2: final current, tau", abs(i_end / i_lr - 1) < 0.02 and
          abs(i_tau / (i_lr * (1 - math.exp(-1))) - 1) < 0.03,
          "%.3f A (formula %.3f), at tau %.3f A" % (i_end, i_lr, i_tau))
    ploss_dt = PL["PL_VBUS"] * (DT_TICKS + PL["PL_TD_ON"] - PL["PL_TD_OFF"]) / (2 * ARR)
    check("C07 dead-time voltage loss (pair, i > 0)", 0.0 < ploss_dt < 0.2,
          "%.3f V of %.0f V = (DT + ton - toff) / 2 ARR" % (ploss_dt, PL["PL_VBUS"]))
    print()

    # [3] closed loop: averaged model vs host test (switch-level plant)
    host = HOST_LOG.read_text() if HOST_LOG.exists() else ""
    hm = host_metrics(host)
    print("[3] Closed loop: Python averaged model vs C firmware on the switch-level plant")
    events = [(0.0, "cmd", 2000.0), (0.0, "load", 0.05), (1.0, "load", 0.20)]
    res, drv = simulate(events, 1.5)
    py = {
        "s1_t_settle_s": last_outside(res, 0.0, 0.8, 2000.0),
        "s1_rpm_pk": window(res, "rpm", 0.0, 0.8).max(),
        "s1_rpm_ss": window(res, "rpm", 0.8, 1.0).mean(),
        "s1_isrc_a": window(res, "isrc", 0.8, 1.0).mean(),
        "s2_rpm_min": window(res, "rpm", 1.0, 1.3).min(),
        "s2_t_rec_s": last_outside(res, 1.0, 1.3, 2000.0) - 1.0,
        "s2_isrc_a": window(res, "isrc", 1.3, 1.5).mean(),
    }
    have = all(k in hm for k in py)
    def cmp(code, name, key, tol, unit, fmt="%.3f"):
        c = hm.get(key, float("nan"))
        p = py[key]
        ok = have and abs(p - c) <= tol
        check("%s %s" % (code, name), ok, ("Python " + fmt + ", C " + fmt + " %s (tol %s)")
              % (p, c, unit, (fmt % tol).strip()))
    cmp("C08", "S1 settling time to +/-1 %", "s1_t_settle_s", 0.05, "s")
    cmp("C09", "S1 peak speed", "s1_rpm_pk", 15.0, "rpm", "%.1f")
    cmp("C10", "S1 supply current at 2000 rpm, 0.05 N m", "s1_isrc_a", 0.03, "A")
    cmp("C11", "S2 lowest speed after 0.20 N m step", "s2_rpm_min", 40.0, "rpm", "%.0f")
    cmp("C12", "S2 recovery to +/-1 %", "s2_t_rec_s", 0.03, "s")
    cmp("C13", "S2 supply current at 0.20 N m", "s2_isrc_a", 0.05, "A")
    print()

    # [4] dead time: SPICE leg
    spice = SPICE_LOG.read_text() if SPICE_LOG.exists() else ""
    print("[4] Dead time (ngspice: %s)" % LEG_DECK.relative_to(HERE))
    try:
        sweep, delay, six, bemf = spice_values(spice)
    except AttributeError:
        sweep, delay, six, bemf = {}, {}, (math.nan,) * 3, (math.nan,) * 2
    if sweep:
        loss = {round(si(k) * 1e9): v[0] + v[1] for k, v in sweep.items()}     # key: dead time in ns
        base = loss[1000]
        safe = min(dt for dt in loss if all(loss[d2] <= 1.25 * base for d2 in loss if d2 >= dt))
        check("C14 DT = 0: shoot-through", sweep["0"][2] > 100 and loss[0] > 20 * loss[500],
              "%.0f A peak, %.0f uJ vs %.1f uJ at 500 ns" % (sweep["0"][2], loss[0] * 1e6, loss[500] * 1e6))
        check("C15 shortest safe dead time < firmware DT", safe < CFG["BLDC_DT_NS"],
              "%d ns (loss within 25 %% of DT = 1 us), firmware %.0f ns" % (safe, CFG["BLDC_DT_NS"]))
        on_min = min(delay["td_on_h"], delay["td_on_l"])
        off_max = max(delay["td_off_h"], delay["td_off_l"])
        check("C16 plant switch delays bound SPICE (pessimistic)",
              PL["PL_TD_ON"] * TICK <= on_min and PL["PL_TD_OFF"] * TICK >= off_max,
              "on %.1f <= %.1f ns, off %.1f >= %.1f ns" % (PL["PL_TD_ON"] * TICK * 1e9, on_min * 1e9,
                                                          PL["PL_TD_OFF"] * TICK * 1e9, off_max * 1e9))
        gap_pl = hm.get("s1_gap_ns", math.nan)
        gap_sp = min(delay["gap_lh"], delay["gap_hl"]) * 1e9
        check("C17 both-off gap: SPICE gates >= plant switches", gap_sp >= gap_pl,
              "SPICE %.0f ns, host test %.1f ns" % (gap_sp, gap_pl))
    else:
        check("C14-C17 SPICE log present", False, "run sh sim/run_spice.sh")
        safe = math.nan
    print()

    # [5] full inverter: SPICE vs host-test S0
    print("[5] Fixed operating point, 2000 rpm, duty 0.75: ngspice inverter vs host-test plant")
    s0 = (hm.get("s0_isrc_a", math.nan), hm.get("s0_ia_rms", math.nan), hm.get("s0_ia_pk", math.nan))
    res0, _ = simulate([], 0.035, hold_w=2000.0 / RPM, open_loop_duty=0.75, sub=20)
    m0 = res0["t"] > 0.020
    py0 = (res0["isrc"][m0].mean(), math.sqrt(np.mean(res0["ia"][m0] ** 2)))
    for code, name, k in (("C18", "supply current", 0), ("C19", "phase A rms current", 1), ("C20", "phase A peak current", 2)):
        check("%s %s: SPICE vs plant" % (code, name), abs(six[k] / s0[k] - 1) < 0.02,
              "SPICE %.3f, plant %.3f A (%+.2f %%)" % (six[k], s0[k], 100 * (six[k] / s0[k] - 1)))
    check("C21 averaged model at the same point", abs(py0[0] / s0[0] - 1) < 0.03 and abs(py0[1] / s0[1] - 1) < 0.03,
          "Isrc %.3f, Ia rms %.3f A" % py0)
    e_flat = PL["PL_KE_PH"] * 2000.0 / RPM
    check("C22 SPICE back-EMF flat top = Ke x w", abs(bemf[0] / e_flat - 1) < 0.005 and abs(bemf[1] / (2 * e_flat) - 1) < 0.005,
          "phase %.3f V, line %.3f V (Ke w = %.3f V)" % (bemf[0], bemf[1], e_flat))
    print()

    # [6] logs of the other gates
    print("[6] Host test, negative gate, KiCad")
    m = re.search(r"RESULT: (\d+)/(\d+) PASS", host)
    res_h = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
    check("C23 host test log RESULT all PASS", res_h[0] == res_h[1], "%d/%d" % res_h)
    neg = NEG_LOG.read_text() if NEG_LOG.exists() else ""
    caught = len(re.findall(r"test FAILED as required", neg))
    check("C24 negative gate log: every mutant caught", caught == 7 and "NEGATIVE GATE: 7/7" in neg,
          "%d mutants" % caught)
    sch = SCHEMATIC.read_text() if SCHEMATIC.exists() else ""
    vals = dict(re.findall(r'\(property "Reference" "(\w+)".*?\(property "Value" "([^"]+)"', sch, re.S))
    sch_ok = (vals.get("RS1") == "2.5m" and vals.get("U4") == "INA181A1" and vals.get("R51") == "100k"
              and vals.get("R52") == "10k" and vals.get("R11") == "10" and vals.get("R12") == "10"
              and sum(1 for k, v in vals.items() if k.startswith("Q") and v == "IRFR4104") == 6)
    div = (100e3 + 10e3) / 10e3
    check("C25 schematic values = firmware and SPICE", sch_ok and div == CFG["BLDC_VBUS_DIV"],
          "%d parts; shunt %s, divider 1/%.0f, Rg %s ohm" % (sum(1 for k in vals if re.fullmatch(r"[A-Z]+\d+", k)),
                                                              vals.get("RS1"), div, vals.get("R11")))
    erc = ERC_LOG.read_text() if ERC_LOG.exists() else ""
    nets = re.search(r"RESULT: PASS \((\d+) nets checked\)", erc)
    check("C26 ERC 0 errors 0 warnings, netlist check PASS", "Errors 0  Warnings 0" in erc and nets is not None,
          "%s nets checked" % (nets.group(1) if nets else "?"))
    print()

    print("  S1/S2 (Python): settle %.3f s, peak %.1f rpm, dip to %.0f rpm, Isrc %.3f / %.3f A"
          % (py["s1_t_settle_s"], py["s1_rpm_pk"], py["s2_rpm_min"], py["s1_isrc_a"], py["s2_isrc_a"]))
    fails = RESULTS.count(False)
    print()
    print("RESULT: %d/%d PASS" % (len(RESULTS) - fails, len(RESULTS)))
    if not args.no_plot:
        plot(res, sweep)
        print("  wrote %s" % FIGURE_PATH.relative_to(HERE))
    return 1 if fails else 0


def read_trace(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return rows


def plot(res, sweep):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(2, 3, figsize=(16, 9))
    a = ax[0, 0]
    a.plot(res["t"], res["rpm"], label="speed (Python averaged model)")
    a.plot(res["t"], res["rpm_ref"], "--", label="ramped reference")
    a.axvline(1.0, color="0.6", lw=0.8)
    a.set(xlabel="time (s)", ylabel="rpm", title="Start to 2000 rpm, load step 0.05 -> 0.20 N m at 1.0 s")
    a.legend(loc="lower right", fontsize=8)
    a = ax[1, 0]
    a.plot(res["t"], res["i_meas"], lw=0.4, color="0.75", label="sampled DC-link current (dips at each commutation)")
    avg = np.convolve(res["i_meas"], np.ones(20) / 20, mode="same")
    a.plot(res["t"], avg, lw=1.0, label="sampled current, 1 ms average")
    a.plot(res["t"], res["i_ref"], lw=1.0, label="current reference")
    a.set(xlabel="time (s)", ylabel="A", title="Cascaded loops (Python model)")
    a.legend(fontsize=8)

    if HIL_TRACE.exists():
        rows = read_trace(HIL_TRACE)
        t = np.array([float(r["t_s"]) for r in rows]) * 1e3
        t -= t[0]
        a = ax[0, 1]
        for k, n in (("ia", "A"), ("ib", "B"), ("ic", "C")):
            a.plot(t, [float(r[k]) for r in rows], lw=0.8, label="i" + n)
        a.set(xlabel="time (ms)", ylabel="A", title="C firmware + switch-level plant: phase currents, 2000 rpm")
        a.legend(fontsize=8)
        a = ax[1, 1]
        va = np.array([float(r["va"]) for r in rows])
        a.plot(t, va, lw=0.3, color="0.6", label="terminal A (PWM)")
        ea = np.array([float(r["ea"]) for r in rows]) + np.array([float(r["vn"]) for r in rows])
        a.plot(t, ea, lw=1.2, label="back-EMF A + neutral")
        a.plot(t, [float(r["ea"]) for r in rows], lw=1.0, label="back-EMF A")
        a.set(xlabel="time (ms)", ylabel="V", title="Phase A: floating sectors show the back-EMF")
        a.legend(fontsize=8, loc="lower left")
    if PWM_TRACE.exists():
        rows = read_trace(PWM_TRACE)
        t = (np.array([float(r["t_s"]) for r in rows]) - float(rows[0]["t_s"])) * 1e6
        a = ax[0, 2]
        # the PWM leg = the one whose high-side gate switches in this window
        legs = [x for x in range(3) if len({r["gates_AhAlBhBlChCl"][2 * x] for r in rows}) > 1]
        x = legs[0] if legs else 0
        name = "ABC"[x]
        sig = [("gate H", "gates_AhAlBhBlChCl", 2 * x), ("gate L", "gates_AhAlBhBlChCl", 2 * x + 1),
               ("switch H", "sw_AhAlBhBlChCl", 2 * x), ("switch L", "sw_AhAlBhBlChCl", 2 * x + 1)]
        for k, (lab, col, pos) in enumerate(sig):
            a.step(t, [int(r[col][pos]) * 0.8 + 1.2 * (3 - k) for r in rows], where="post", label=lab)
        a.set(xlabel="time (us)", yticks=[], title="Leg %s, two PWM periods (31.25 ns ticks)" % name)
        a.legend(fontsize=8, loc="upper right")
        edges = [i for i in range(1, len(rows)) if rows[i]["gates_AhAlBhBlChCl"][2 * x] == "1"
                 and rows[i - 1]["gates_AhAlBhBlChCl"][2 * x] == "0"]
        if edges:
            i0 = edges[0]
            a.set_xlim(t[i0] - 1.5, t[i0] + 1.5)
            a.set_title("Leg %s, rising edge: 500 ns dead time, switch delays" % name)
    if sweep:
        a = ax[1, 2]
        dts = sorted(sweep, key=si)
        a.semilogy([si(d) * 1e9 for d in dts], [(sweep[d][0] + sweep[d][1]) * 1e6 for d in dts], "o-",
                   label="leg loss per PWM period (both edges)")
        a.semilogy([si(d) * 1e9 for d in dts], [sweep[d][2] for d in dts], "s--", label="peak low-side drain current (A)")
        a.axvline(CFG["BLDC_DT_NS"], color="0.5", ls=":", label="firmware dead time")
        a.set(xlabel="dead time (ns)", title="ngspice: one leg, 8 A, 24 V")
        a.legend(fontsize=8)
    fig.tight_layout()
    FIGURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE_PATH, dpi=110)


if __name__ == "__main__":
    sys.exit(main())
