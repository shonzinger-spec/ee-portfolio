#!/usr/bin/env python3
"""Digital synchronous buck (project 02): standalone model of the design.

12 V -> 5 V / 3 A, 200 kHz, ESP32-class digital PI loop at 50 kHz with input-voltage
feed-forward. Needs Python 3.9+ and numpy; matplotlib is used only for the figure.

    python3 main.py             # self-check table + figure docs/img/main_py_buck.png
    python3 main.py --no-plot   # self-check table only (matplotlib not needed)

Exit code 0 when every check passes, 1 when any check fails.

What is modelled
  Power stage : design equations (duty, ripple, Isat, capacitor sizing), the output ripple
                from a Fourier series of the triangular inductor current through the real
                capacitor bank, and a loss budget (conduction, switching, dead time, reverse
                recovery, snubber, gate drive) built from the same VDMOS card as the SPICE decks.
  Control     : the digital loop gain (PI + one-sample computation delay + zero-order hold +
                anti-alias filter + averaged LC plant) at several loads and component corners,
                and a Python port of src/buck_ctrl run against the averaged plant, compared with
                the C host test.
  Checks      : the model against the LTspice and ngspice logs in docs/, LTspice against
                ngspice, and the C host-test log.

What is not modelled
  PCB parasitics beyond the 1 nH commutation loop, magnetics core loss, temperature
  dependence of Rds(on), ADC noise and nonlinearity, and the real ESP32 timing.
  Everything here is simulation; no hardware was built.

Author: Shon Zinger. See README.md, section Credits, for the AI assistance note.
"""
import argparse
import pathlib
import re
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
FIGURE_PATH = HERE / "docs" / "img" / "main_py_buck.png"
CONFIG_H = HERE / "src" / "buck_ctrl" / "buck_config.h"
LT_NETLIST = HERE / "sim" / "buck_sync.cir"
LT_LOG = HERE / "docs" / "sim_ltspice.log"
NG_LOG = HERE / "docs" / "sim_ngspice.log"
HOST_LOG = HERE / "docs" / "host_test.log"

# --------------------------------------------------------------------------
# Power stage, mirrored from sim/buck_sync.cir (check C01 compares them)
# --------------------------------------------------------------------------
VIN, FSW = 12.0, 200e3
D_SPICE = 0.4195                      # fixed duty of the open-loop SPICE runs
TD, TE = 50e-9, 10e-9                 # dead time, driver edge time
VDH, VDL, RDRV = 11.3, 12.0, 3.1      # gate drive levels; driver R + 2.2 ohm gate resistor
L, DCR = 15e-6, 0.020
C7, ESR7, C8, ESR8 = 470e-6, 0.010, 10e-6, 0.003
RSH = 0.005
CIN_ESR = 1.0 / (1.0 / 0.003 + 1.0 / 0.150)   # 20 uF ceramic || 100 uF electrolytic
LLOOP, RSN, CSN = 1e-9, 1.8, 1e-9
# Generic 40 V N-MOSFET VDMOS card (same in both decks)
FET = dict(vto=1.8, kp=60.0, rd=3e-3, rs=2e-3, rg=1.2, rb=4e-3, cgs=1.5e-9, cgdmax=0.8e-9,
           cgdmin=40e-12, isat=5e-12, n=1.05, tt=5e-9)
BVDSS = 40.0
ISAT_L = 6.0                          # inductor saturation current rating, A
RTH_JA_FET, RTH_L, T_AMB = 50.0, 40.0, 50.0   # degC/W (SO-8 class on 1 in^2), ambient degC
LOADS_A = {"full 3 A": 1.6667, "light 1 A": 5.0}   # SPICE case 1 and 2 (load resistance)


def read_config(path):
    """#define NAME value pairs from buck_config.h (numbers and simple expressions)."""
    raw = {}
    for m in re.finditer(r"#define\s+(BUCK_\w+)\s+(.+)", path.read_text()):
        raw[m.group(1)] = re.sub(r"/\*.*?\*/", "", m.group(2)).strip()
    env, out = {}, {}
    for k, v in raw.items():
        expr = re.sub(r"(\d+\.?\d*(?:e-?\d+)?)f\b", r"\1", v)
        try:
            out[k] = float(eval(expr, {"__builtins__": {}}, env))
            env[k] = out[k]
        except Exception:
            pass
    return out


CFG = read_config(CONFIG_H)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
RESULTS = []


def check(name, ok, detail):
    RESULTS.append(bool(ok))
    print("  %-46s %-44s %s" % (name, detail, "PASS" if ok else "FAIL"))


def rds_on(vgs):
    return FET["rd"] + FET["rs"] + 1.0 / (FET["kp"] * (vgs - FET["vto"]))


def vf_diode(i):
    vt = 0.025852
    return FET["n"] * vt * np.log(i / FET["isat"]) + i * FET["rb"]


def zcap(f):
    s = 2j * np.pi * f
    z7 = 1 / (s * C7) + ESR7
    z8 = 1 / (s * C8) + ESR8
    return 1 / (1 / z7 + 1 / z8)


# --------------------------------------------------------------------------
# Steady-state power stage
# --------------------------------------------------------------------------
def gate_timing(i):
    """Turn-on/turn-off delays of the high-side switch and its Miller time, from the card."""
    rgt = RDRV + FET["rg"]
    ciss = FET["cgs"] + FET["cgdmax"]
    tau = rgt * ciss
    vpl = FET["vto"] + np.sqrt(2 * i / FET["kp"])            # plateau (square law)
    qgd = 0.5 * (FET["cgdmax"] + FET["cgdmin"]) * 2.0 + FET["cgdmin"] * (VIN - 2.0)
    t_rise = qgd / ((VDH - vpl) / rgt)                         # Miller time at turn-on
    t_fall = qgd / (vpl / rgt)                                 # Miller time at turn-off
    td_on = tau * np.log(VDH / (VDH - vpl))
    td_off = tau * np.log(VDH / vpl)
    return td_on, td_off, t_rise, t_fall, qgd


def operating_point(rload, d=D_SPICE):
    """Average output voltage and current at fixed duty d (open loop, as in the SPICE runs)."""
    vout = 5.0
    for _ in range(50):
        i = vout / rload
        td_on, td_off, t_rise, t_fall, _ = gate_timing(i)
        d_eff = d + (td_off + 0.5 * t_fall - td_on - 0.5 * t_rise) * FSW
        r_sw = d_eff * rds_on(VDH) + (1 - d_eff) * rds_on(VDL)
        v_dt = vf_diode(i) * 2 * TD * FSW                      # body diode during dead times
        vsw = d_eff * VIN - v_dt
        vout_new = vsw * rload / (rload + RSH + r_sw + DCR)
        if abs(vout_new - vout) < 1e-9:
            break
        vout = vout_new
    i = vout / rload
    dil = (VIN - (vout + i * (RSH + DCR))) * d_eff / (L * FSW)
    return dict(vout=vout, iout=i, d_eff=d_eff, dil=dil, r_sw=r_sw)


def ripple_pp(i_avg, dil, d, rload, nharm=60):
    """Output ripple p-p from the triangular inductor current through Zcap || (Rsh + R)."""
    t = np.linspace(0, 1 / FSW, 2001)
    ph = (t * FSW) % 1.0
    tri = np.where(ph < d, -dil / 2 + dil * ph / d, dil / 2 - dil * (ph - d) / (1 - d))
    v = np.zeros_like(t)
    for k in range(1, nharm + 1):
        ck = 2 * np.mean(tri * np.exp(-2j * np.pi * k * FSW * t))
        f = k * FSW
        zc = zcap(f)
        zout = 1 / (1 / zc + 1 / (RSH + rload))
        v += np.real(ck * zout * rload / (rload + RSH) * np.exp(2j * np.pi * f * t))
    return v.max() - v.min(), t, tri + i_avg, v


def losses(rload):
    op = operating_point(rload)
    i, dil, d = op["iout"], op["dil"], op["d_eff"]
    irms2 = i ** 2 + dil ** 2 / 12
    td_on, td_off, t_rise, t_fall, qgd = gate_timing(i)
    p = {
        "HS conduction": irms2 * rds_on(VDH) * d,
        "LS conduction": irms2 * rds_on(VDL) * (1 - d),
        "inductor DCR": irms2 * DCR,
        "shunt": i ** 2 * RSH,
        "Cin ESR": i ** 2 * d * (1 - d) * CIN_ESR,
        "Cout ESR": dil ** 2 / 12 * ESR7,
        "HS switching": 0.5 * VIN * i * (t_rise + t_fall) * FSW,
        "dead-time diode": vf_diode(i) * i * 2 * TD * FSW,
        "reverse recovery": FET["tt"] * (i - dil / 2) * VIN * FSW,
        "snubber": CSN * VIN ** 2 * FSW,
    }
    pout = op["vout"] ** 2 / rload
    ploss = sum(p.values())
    # gate charge per switch: Cgs x Vdrive + Qgd (Miller), delivered once per period
    p_gate = ((FET["cgs"] * VDH + qgd) * VDH + (FET["cgs"] * VDL + qgd) * VDL) * FSW
    return op, p, pout, pout / (pout + ploss), p_gate


# --------------------------------------------------------------------------
# Digital loop gain
# --------------------------------------------------------------------------
TS = CFG["BUCK_TS_S"]
H_DIV = CFG["BUCK_VDIV_RATIO"]
TAU_AA = (10e3 * 6.8e3 / 16.8e3) * 1e-9


def plant_vsw_to_vload(f, rload, lval=L, cscale=1.0):
    s = 2j * np.pi * f
    z7 = 1 / (s * C7 * cscale) + ESR7
    z8 = 1 / (s * C8 * cscale) + ESR8
    zc = 1 / (1 / z7 + 1 / z8)
    zp = 1 / (1 / zc + 1 / (rload + RSH))
    rs = DCR + rds_on(VDH)
    return zp / (s * lval + rs + zp) * rload / (rload + RSH)


def loop_gain(f, rload, lval=L, cscale=1.0):
    """PI on volts, duty = (Vref + PI) / Vin: the Vin feed-forward cancels the modulator gain,
    the Vref feed-forward is outside the loop. One-sample delay + ZOH + anti-alias pole."""
    w = 2 * np.pi * f
    z = np.exp(1j * w * TS)
    c = CFG["BUCK_KP"] + CFG["BUCK_KI"] * TS / (1 - 1 / z)
    zoh = (1 - np.exp(-1j * w * TS)) / (1j * w * TS)
    # the firmware rescales the ADC count to volts, so the divider gain cancels (sense gain 1)
    return c / z * zoh * plant_vsw_to_vload(f, rload, lval, cscale) / (1 + 1j * w * TAU_AA)


def margins(rload, lval=L, cscale=1.0):
    f = np.logspace(0, np.log10(0.499 / TS), 40000)
    t = loop_gain(f, rload, lval, cscale)
    mag = np.abs(t)
    ph = np.unwrap(np.angle(t)) * 180 / np.pi
    ic = np.where(mag < 1)[0][0]
    ip = np.where(ph < -180)[0]
    gm = -20 * np.log10(mag[ip[0]]) if len(ip) else np.inf
    return f[ic], 180 + ph[ic], gm, (f[ip[0]] if len(ip) else np.nan)


# --------------------------------------------------------------------------
# Python port of src/buck_ctrl (float32 like the MCU) + averaged plant (as test/host/plant.c)
# --------------------------------------------------------------------------
F32 = np.float32


class Ctrl:
    def __init__(self, adc_v):
        self.vref = F32(adc_v * CFG["BUCK_V_PER_COUNT"])
        self.integ = F32(0)
        self.duty = 0
        self.soft = True

    def update(self, adc_v, adc_vin):
        c = CFG
        v = F32(adc_v) * F32(c["BUCK_V_PER_COUNT"])
        vin = max(F32(adc_vin) * F32(c["BUCK_VIN_PER_COUNT"]), F32(c["BUCK_VIN_FF_MIN_V"]))
        if self.soft:
            rem = F32(F32(c["BUCK_VOUT_REF_V"]) - self.vref)
            step = min(F32(c["BUCK_SOFTSTART_V_PER_S"] * c["BUCK_TS_S"]),
                       F32(rem * F32(c["BUCK_TS_S"] / c["BUCK_SOFTSTART_TAU_S"])))
            self.vref = F32(self.vref + step)
            if rem - step < F32(c["BUCK_SOFTSTART_DONE_V"]):
                self.vref, self.soft = F32(c["BUCK_VOUT_REF_V"]), False
        e = F32(self.vref - v)
        e_int = F32(0) if abs(e) < F32(c["BUCK_DEADBAND_V"]) else e
        kits = F32(c["BUCK_KI"] * c["BUCK_TS_S"])
        u_lo, u_hi = F32(c["BUCK_DUTY_MIN"]) * vin, F32(c["BUCK_DUTY_MAX"]) * vin
        u_try = self.vref + F32(c["BUCK_KP"]) * e + self.integ + kits * e_int
        if not ((u_try > u_hi and e_int > 0) or (u_try < u_lo and e_int < 0)):
            self.integ = F32(self.integ + kits * e_int)
        self.integ = min(max(self.integ, F32(u_lo - self.vref)), F32(u_hi - self.vref))
        duty = min(max((self.vref + F32(c["BUCK_KP"]) * e + self.integ) / vin, F32(c["BUCK_DUTY_MIN"])),
                   F32(c["BUCK_DUTY_MAX"]))
        target = duty * F32(c["BUCK_PWM_TICKS"])
        if abs(target - F32(self.duty)) > F32(c["BUCK_DPWM_HYST_LSB"]):
            self.duty = int(target + F32(0.5))
        return self.duty


def adc(v):
    return int(min(max(np.floor(v / CFG["BUCK_ADC_FS_V"] * CFG["BUCK_ADC_COUNTS"] + 0.5), 0),
                   CFG["BUCK_ADC_COUNTS"] - 1))


def closed_loop(t_end=55e-3, events=((20e-3, 10.0), (30e-3, 1.6667), (45e-3, 10.0))):
    """Soft start at 1 A, then the host-test load steps. Returns t, vload arrays."""
    rs_ser, c_f, esr = DCR + 0.0068, C7 + C8, ESR7
    il = vc = vaa = 0.0
    rload, vin = 5.0, VIN
    sub = 40
    dt = TS / sub

    def vout_of(il_, vc_):
        return (il_ + vc_ / esr) / (1 / esr + 1 / (RSH + rload))

    def deriv(il_, vc_, d):
        vo = vout_of(il_, vc_)
        return (d * vin - il_ * rs_ser - vo) / L, (il_ - vo / (RSH + rload)) / c_f

    ctl = Ctrl(adc(0.0))
    duty_applied, ev = 0, list(events)
    n = int(round(t_end / TS))
    t_out, v_out = np.empty(n), np.empty(n)
    k_aa = 1 - np.exp(-dt / TAU_AA)
    for k in range(n):
        t = k * TS
        while ev and ev[0][0] <= t + 1e-12:
            rload = ev.pop(0)[1]
        nxt = ctl.update(adc(vaa), adc(vin * CFG["BUCK_VINDIV_RATIO"]))
        d = duty_applied / CFG["BUCK_PWM_TICKS"]
        for _ in range(sub):
            k1 = deriv(il, vc, d)
            k2 = deriv(il + 0.5 * dt * k1[0], vc + 0.5 * dt * k1[1], d)
            k3 = deriv(il + 0.5 * dt * k2[0], vc + 0.5 * dt * k2[1], d)
            k4 = deriv(il + dt * k3[0], vc + dt * k3[1], d)
            il += dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
            vc += dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
            vload = vout_of(il, vc) * rload / (RSH + rload)
            vaa += (vload * H_DIV - vaa) * k_aa
        duty_applied = nxt
        t_out[k], v_out[k] = t + TS, vout_of(il, vc) * rload / (RSH + rload)
    return t_out, v_out


# --------------------------------------------------------------------------
# Log parsers
# --------------------------------------------------------------------------
def parse_ltspice(path):
    out, name = {}, None
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"Measurement: (\w+)", line)
        if m:
            name = m.group(1)
            out[name] = []
            continue
        m = re.match(r"\s+(\d+)\s+([-\d.eE+]+)", line)
        if name and m:
            out[name].append(float(m.group(2)))
    return out


def parse_ngspice(path):
    out, step = {}, 0
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"step (\d+)", line)
        if m:
            step = int(m.group(1))
            continue
        m = re.match(r"(\w+)\s*=\s*([-\d.eE+]+)", line)
        if step and m:
            out.setdefault(m.group(1), [None, None, None])[step - 1] = float(m.group(2))
    return out


def parse_host(path):
    txt = path.read_text()
    g = lambda pat: float(re.search(pat, txt).group(1))
    return dict(dip=g(r"step dip.*?min ([\d.]+) V"), peak=g(r"release peak.*?max ([\d.]+) V"),
                result=re.search(r"RESULT: (\d+)/(\d+) PASS", txt).groups())


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    print("Sync buck 02 - main.py (simulated design; no hardware built)")
    print("Constants from src/buck_ctrl/buck_config.h: Kp %.3g V/V, Ki %.4g V/(V s), Ts %.0f us,"
          " DPWM %d ticks, dead band %.0f mV, hysteresis %.2f LSB"
          % (CFG["BUCK_KP"], CFG["BUCK_KI"], TS * 1e6, CFG["BUCK_PWM_TICKS"],
             CFG["BUCK_DEADBAND_V"] * 1e3, CFG["BUCK_DPWM_HYST_LSB"]))

    print("\n[1] Netlist and firmware constants")
    net = LT_NETLIST.read_text()
    getp = lambda pat: re.search(pat, net).group(1)
    same = (float(getp(r"D=([\d.]+)")) == D_SPICE and getp(r"L1 sw lx (\S+)") == "15u"
            and getp(r"C7 out c7x (\S+)") == "470u" and getp(r"Rc7 c7x 0 (\S+)") == "10m"
            and getp(r"RL1 lx out (\S+)") == "20m" and getp(r"Rsh out vload (\S+)") == "5m"
            and getp(r"TD=(\S+)") == "50n")
    check("C01 model values = sim/buck_sync.cir", same, "D, L, C7, ESR, DCR, Rsh, dead time")
    fw_ok = (CFG["BUCK_PWM_TICKS"] == 400 and abs(CFG["BUCK_FSW_HZ"] - FSW) < 1
             and abs(CFG["BUCK_DEADTIME_TICKS"] * 12.5e-9 - TD) < 1e-12)
    check("C02 firmware fsw / ticks / dead time", fw_ok, "200 kHz, 400 ticks, 4 x 12.5 ns = 50 ns")

    print("\n[2] Design equations (3 A, 12 V)")
    d0 = 5.0 / VIN
    dil0 = (VIN - 5.0) * d0 / (L * FSW)
    ipk = 3.0 + dil0 / 2
    f0 = 1 / (2 * np.pi * np.sqrt(L * (C7 + C8)))
    z0 = np.sqrt(L / (C7 + C8))
    print("    D = %.4f, dIL = %.3f A p-p (%.0f %% of 3 A), Ipk = %.2f A, f0 = %.0f Hz, Z0 = %.3f ohm"
          % (d0, dil0, dil0 / 3 * 100, ipk, f0, z0))
    check("C03 inductor ripple 20-40 % of Iout", 0.2 <= dil0 / 3 <= 0.4, "%.0f %%" % (dil0 / 3 * 100))
    check("C04 Ipk <= Isat / 1.2", ipk <= ISAT_L / 1.2, "%.2f A vs %.2f A" % (ipk, ISAT_L / 1.2))
    check("C05 2.5 A step: Z0 * dI <= 10 % Vout", z0 * 2.5 <= 0.5, "%.0f mV (limit 500 mV)" % (z0 * 2.5e3))
    check("C06 LC resonance > 10 x loop crossover", f0 > 10 * margins(5.0)[0],
          "f0 %.0f Hz, fc %.0f Hz" % (f0, margins(5.0)[0]))

    lt, ng = parse_ltspice(LT_LOG), parse_ngspice(NG_LOG)
    print("\n[3] LTspice vs ngspice (cases: 1 = 3 A, 2 = 1 A, 3 = 3 A no snubber)")
    tol = {"vout_avg": ("rel", 0.002), "il_pp": ("rel", 0.02), "vout_pp": ("rel", 0.05),
           "pin": ("rel", 0.005), "pout": ("rel", 0.005), "eff": ("abs", 0.003), "pgate": ("rel", 0.03)}
    for key, (kind, lim) in tol.items():
        for c in (0, 1):
            a, b = lt[key][c], ng[key][c]
            err = abs(a - b) / abs(a) if kind == "rel" else abs(a - b)
            check("C07 %s case %d" % (key, c + 1), err <= lim,
                  "LT %.5g  ng %.5g  %s %.3g%s" % (a, b, "rel" if kind == "rel" else "abs",
                                                    err * (100 if kind == "rel" else 1),
                                                    " %" if kind == "rel" else ""))
    for name, d in (("LTspice", lt), ("ngspice", ng)):
        check("C08 %s Vsw peak < 0.75 BVdss" % name, max(d["vsw_max"]) < 0.75 * BVDSS,
              "max %.1f V (limit %.0f V)" % (max(d["vsw_max"]), 0.75 * BVDSS))
        check("C09 %s snubber lowers the Vsw peak" % name, d["vsw_max"][0] < d["vsw_max"][2],
              "%.1f V with, %.1f V without" % (d["vsw_max"][0], d["vsw_max"][2]))

    print("\n[4] Model vs SPICE")
    eff_model = {}
    for c, (label, rl) in enumerate(LOADS_A.items()):
        op, p, pout, eff, pg = losses(rl)
        eff_model[label] = eff
        vpp, *_ = ripple_pp(op["iout"], op["dil"], op["d_eff"], rl)
        for name, d in (("LT", lt), ("ng", ng)):
            check("C10 Vout %s vs %s" % (label, name), abs(op["vout"] - d["vout_avg"][c]) / d["vout_avg"][c] <= 0.01,
                  "model %.4f V, SPICE %.4f V" % (op["vout"], d["vout_avg"][c]))
            check("C11 dIL p-p %s vs %s" % (label, name), abs(op["dil"] - d["il_pp"][c]) / d["il_pp"][c] <= 0.05,
                  "model %.3f A, SPICE %.3f A" % (op["dil"], d["il_pp"][c]))
            check("C12 Vout ripple %s vs %s" % (label, name), abs(vpp - d["vout_pp"][c]) / d["vout_pp"][c] <= 0.20,
                  "model %.2f mV, SPICE %.2f mV" % (vpp * 1e3, d["vout_pp"][c] * 1e3))
            check("C13 efficiency %s vs %s" % (label, name), abs(eff - d["eff"][c]) <= 0.01,
                  "model %.2f %%, SPICE %.2f %%" % (eff * 100, d["eff"][c] * 100))
            check("C14 gate drive %s vs %s" % (label, name), abs(pg - d["pgate"][c]) / d["pgate"][c] <= 0.25,
                  "model %.0f mW, SPICE %.0f mW" % (pg * 1e3, d["pgate"][c] * 1e3))
            ps = CSN * VIN ** 2 * FSW
            check("C15 snubber loss %s vs %s" % (label, name), 0.5 * ps <= d["psnub"][c] <= 1.5 * ps,
                  "C Vin^2 fsw %.1f mW (+/-50 %%), SPICE %.1f mW" % (ps * 1e3, d["psnub"][c] * 1e3))
        if c == 0:
            print("    loss budget at 3 A (mW): " + ", ".join("%s %.0f" % (k, v * 1e3) for k, v in p.items()))
            p_hs = p["HS conduction"] + p["HS switching"] + p["reverse recovery"]
            p_ls = p["LS conduction"] + p["dead-time diode"]
            tj = T_AMB + RTH_JA_FET * max(p_hs, p_ls)
            tl = T_AMB + RTH_L * p["inductor DCR"]
            check("C16 FET Tj at 50 degC ambient < 100 degC", tj < 100, "HS %.0f mW, LS %.0f mW -> Tj %.0f degC"
                  % (p_hs * 1e3, p_ls * 1e3, tj))
            check("C17 inductor temperature < 100 degC", tl < 100, "%.0f mW -> %.0f degC" % (p["inductor DCR"] * 1e3, tl))

    print("\n[5] Digital loop (PI + feed-forward, 50 kHz, 1-sample delay)")
    corners = [("0.01 A", 500.0, L, 1.0), ("1 A", 5.0, L, 1.0), ("3 A", 1.6667, L, 1.0),
               ("0.01 A, L +20 %", 500.0, 1.2 * L, 1.0), ("0.01 A, C -20 %", 500.0, L, 0.8),
               ("0.01 A, L -20 % C +20 %", 500.0, 0.8 * L, 1.2)]
    for label, rl, lv, cs in corners:
        fc, pm, gm, fpc = margins(rl, lv, cs)
        check("C18 margins %s" % label, pm >= 45 and gm >= 6,
              "fc %.0f Hz, PM %.0f deg, GM %.1f dB" % (fc, pm, gm))

    print("\n[6] Python port of the controller vs the C host test")
    t, v = closed_loop()
    host = parse_host(HOST_LOG)
    i30, i45, i55 = (int(round(x / TS)) for x in (30e-3, 45e-3, 55e-3))
    dip, peak = v[i30:i45].min(), v[i45:i55].max()
    check("C19 host test log RESULT all PASS", host["result"][0] == host["result"][1],
          "%s/%s" % host["result"])
    check("C20 0.5 -> 3 A dip: Python vs C", abs(dip - host["dip"]) <= 0.01,
          "Python %.4f V, C %.4f V" % (dip, host["dip"]))
    check("C21 3 -> 0.5 A peak: Python vs C", abs(peak - host["peak"]) <= 0.01,
          "Python %.4f V, C %.4f V" % (peak, host["peak"]))

    fails = RESULTS.count(False)
    print("\nRESULT: %d/%d PASS" % (len(RESULTS) - fails, len(RESULTS)))
    if not args.no_plot:
        plot(lt, ng, eff_model, t, v)
    return 1 if fails else 0


def plot(lt, ng, eff_model, t, v):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(2, 2, figsize=(12, 8))
    op = operating_point(1.6667)
    vpp, tt, il, vr = ripple_pp(op["iout"], op["dil"], op["d_eff"], 1.6667)
    a = ax[0, 0]
    a.plot(tt * 1e6, il, color="C0")
    a.set_xlabel("time (us)"); a.set_ylabel("inductor current (A)", color="C0")
    a2 = a.twinx()
    a2.plot(tt * 1e6, (vr + op["vout"]) * 1, color="C3")
    a2.set_ylabel("Vout (V)", color="C3")
    a.set_title("Model steady state at 3 A: dIL %.2f A p-p, ripple %.1f mV p-p" % (op["dil"], vpp * 1e3))

    a = ax[0, 1]
    loads = np.linspace(0.25, 3.0, 40)
    a.plot(loads, [losses(5.0 / i)[3] * 100 for i in loads], label="model (loss budget)")
    il_pts = [3.0, 1.0]
    a.plot(il_pts, [x * 100 for x in lt["eff"][:2]], "o", label="LTspice")
    a.plot(il_pts, [x * 100 for x in ng["eff"][:2]], "x", ms=10, label="ngspice")
    a.set_xlabel("load current (A)"); a.set_ylabel("power-stage efficiency (%)")
    a.set_title("Efficiency (gate drive excluded)"); a.legend(); a.grid(alpha=0.3)

    a = ax[1, 0]
    f = np.logspace(0, np.log10(0.499 / TS), 2000)
    for label, rl in (("0.01 A", 500.0), ("1 A", 5.0), ("3 A", 1.6667)):
        tg = loop_gain(f, rl)
        a.semilogx(f, 20 * np.log10(np.abs(tg)), label="|T| %s" % label)
    a.axhline(0, color="k", lw=0.5)
    a.set_xlabel("frequency (Hz)"); a.set_ylabel("loop gain (dB)"); a.grid(alpha=0.3, which="both")
    fc, pm, gm, _ = margins(500.0)
    a.set_title("Digital loop gain: fc %.0f Hz, PM %.0f deg, GM %.1f dB (0.01 A)" % (fc, pm, gm))
    a.legend()

    a = ax[1, 1]
    a.plot(t * 1e3, v)
    a.axhline(5.05, color="k", lw=0.5, ls="--"); a.axhline(4.95, color="k", lw=0.5, ls="--")
    for x, s in ((30, "0.5 -> 3 A"), (45, "3 -> 0.5 A")):
        a.axvline(x, color="grey", lw=0.5); a.text(x + 0.3, 5.35, s)
    a.set_xlabel("time (ms)"); a.set_ylabel("Vload (V)")
    a.set_title("Closed loop, Python port of src/buck_ctrl")
    fig.suptitle("02 - Digital synchronous buck 12 V to 5 V / 3 A (simulated)")
    fig.tight_layout()
    FIGURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE_PATH, dpi=120)
    print("figure: %s" % FIGURE_PATH.relative_to(HERE))


if __name__ == "__main__":
    sys.exit(main())
