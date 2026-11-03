#!/usr/bin/env python3
"""Smart BMS simulator (project 04): standalone model of the pack and the BMS firmware.

4S1P Li-ion pack (3.0 Ah NMC 18650-class cells), BMS firmware stepping at 10 Hz with
cell-voltage, current and temperature protection, coulomb-counting state of charge and
passive balancing. Needs Python 3.9+ and numpy; matplotlib is used only for the figure.

    python3 main.py             # self-check table + docs/img/main_py_bms.png + docs/charge_log.csv
    python3 main.py --no-plot   # self-check table and CSV only (matplotlib not needed)

Exit code 0 when every check passes, 1 when any check fails.

What is modelled
  Cell     : second-order Thevenin equivalent circuit (R0 + two RC pairs) on a 25 degC
             OCV(SoC) table, resistance rising with cold (x2 at 0 degC), a lumped thermal
             mass to ambient, an overcharge side reaction above 100 % SoC that turns charge
             current into heat, and one lumped Arrhenius decomposition reaction that drives
             thermal runaway. Cell-to-cell spread in capacity, resistance and initial SoC.
  BMS      : a line-for-line Python port of src/bms/bms.c (integer arithmetic), run in the
             loop with the plant exactly as test/host/test_bms.c runs the C code.
  Constants: read from src/bms/bms_config.h and test/host/plant.h, so the Python model,
             the C firmware and the C plant cannot drift apart.
"""
import argparse
import math
import pathlib
import re
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_H = HERE / "src" / "bms" / "bms_config.h"
PLANT_H = HERE / "test" / "host" / "plant.h"
HOST_LOG = HERE / "docs" / "host_test.log"
NEG_LOG = HERE / "docs" / "host_negative.log"
SPICE_LOG = HERE / "docs" / "sim_ngspice.log"
SPICE_DECK = HERE / "sim" / "ngspice" / "cell_sense.cir"
SCHEMATIC = HERE / "hw" / "kicad" / "bms.kicad_sch"
ERC_LOG = HERE / "docs" / "kicad_erc.log"
FIGURE_PATH = HERE / "docs" / "img" / "main_py_bms.png"
CSV_PATH = HERE / "docs" / "charge_log.csv"


def read_defines(path, prefix):
    """#define NAME value pairs: numbers, or comma lists returned as tuples."""
    out = {}
    for m in re.finditer(r"#define[ \t]+(%s\w+)[ \t]+([^\n]+)" % prefix, path.read_text()):
        v = re.sub(r"/\*.*", "", m.group(2)).strip()
        try:
            nums = [float(x) for x in v.split(",")]
        except ValueError:
            continue
        out[m.group(1)] = tuple(nums) if len(nums) > 1 else nums[0]
    return out


CFG = read_defines(CONFIG_H, "BMS_")
PL = read_defines(PLANT_H, "PLANT_")
N = int(CFG["BMS_NCELLS"])
OCV_MV = [int(x) for x in CFG["BMS_OCV_TABLE_MV"]]
RGAS = 8.314

RESULTS = []


def check(name, ok, detail):
    RESULTS.append(bool(ok))
    print("  %-46s %-44s %s" % (name, detail, "PASS" if ok else "FAIL"))


# --------------------------------------------------------------------------
# Plant: mirror of test/host/plant.c
# --------------------------------------------------------------------------
def ocv(soc):
    if soc >= 1.0:
        return OCV_MV[10] / 1000.0 + PL["PLANT_OC_SLOPE_V"] * (soc - 1.0)
    if soc <= 0.0:
        return OCV_MV[0] / 1000.0 + PL["PLANT_OD_SLOPE_V"] * soc
    x = soc * 10.0
    i = min(int(x), 9)
    return (OCV_MV[i] + (x - i) * (OCV_MV[i + 1] - OCV_MV[i])) / 1000.0


def r_temp(t_c):
    return math.exp(PL["PLANT_R_B_K"] * (1.0 / (t_c + 273.15) - 1.0 / 298.15))


def selfheat_w(t_c):
    """Heat of the decomposition reaction with all reactant left (x_rr = 1)."""
    return PL["PLANT_RR_Q100_W"] * math.exp(
        -PL["PLANT_RR_EA_J_MOL"] / RGAS * (1.0 / (t_c + 273.15) - 1.0 / 373.15))


class Plant:
    def __init__(self, soc0, t_amb):
        self.soc = list(soc0)
        self.v1 = [0.0] * N
        self.v2 = [0.0] * N
        self.t = [float(t_amb)] * N
        self.x = [1.0] * N
        self.vt = [ocv(s) for s in soc0]
        self.ic = [0.0] * N
        self.t_amb, self.rth = float(t_amb), PL["PLANT_RTH_K_W"]
        self.src = "none"
        self.cc, self.cv, self.term = 3.0, 16.8, 0.15
        self.faulty, self.in_cv, self.done, self.term_s = False, False, False, 0.0
        self.load_a = self.load_ohm = 0.0
        self.ip = 0.0
        self.q = PL["PLANT_CELL_Q_AH"]
        self.rs = PL["PLANT_CELL_R_SCALE"]
        self.a_rr = PL["PLANT_RR_Q100_W"] / (
            PL["PLANT_RR_H_J"] * math.exp(-PL["PLANT_RR_EA_J_MOL"] / (RGAS * (100.0 + 273.15))))

    def _substep(self, dt, chg_on, dsg_on, bal):
        e = r = 0.0
        for i in range(N):
            e += ocv(self.soc[i]) + self.v1[i] + self.v2[i]
            r += PL["PLANT_R0_OHM"] * self.rs[i] * r_temp(self.t[i])
        ip = 0.0
        if self.src == "charger" and not self.done:
            if chg_on:
                ip = self.cc
                if not self.faulty:
                    icv = (self.cv - e) / r
                    if icv < ip:
                        ip = icv if icv > 0.0 else 0.0
                        self.in_cv = True
            if not self.faulty and self.in_cv and ip < self.term:
                self.term_s += dt
                if self.term_s >= 1.0:
                    self.done = True
            else:
                self.term_s = 0.0
        elif self.src == "load_i" and dsg_on:
            ip = -self.load_a
        elif self.src == "load_r" and dsg_on:
            ip = -e / (self.load_ohm + r)
        self.ip = ip

        e1, e2 = math.exp(-dt / PL["PLANT_TAU1_S"]), math.exp(-dt / PL["PLANT_TAU2_S"])
        for i in range(N):
            ks = self.rs[i] * r_temp(self.t[i])
            r0, r1, r2 = PL["PLANT_R0_OHM"] * ks, PL["PLANT_R1_OHM"] * ks, PL["PLANT_R2_OHM"] * ks
            oc = ocv(self.soc[i])
            ic = ip
            if bal & (1 << i):
                ic -= (oc + self.v1[i] + self.v2[i]) / PL["PLANT_BLEED_OHM"]
            vt = oc + self.v1[i] + self.v2[i] + ic * r0
            heat = ic * ic * r0 + self.v1[i] ** 2 / r1 + self.v2[i] ** 2 / r2
            f = 0.0
            if ic > 0.0:
                f = min(max((self.soc[i] - 1.0) / PL["PLANT_OC_SIDE_WIDTH"], 0.0), 1.0)
                heat += f * ic * vt
            self.soc[i] += (1.0 - f) * ic * dt / (self.q[i] * 3600.0)
            self.v1[i] = self.v1[i] * e1 + r1 * ic * (1.0 - e1)
            self.v2[i] = self.v2[i] * e2 + r2 * ic * (1.0 - e2)
            k = self.a_rr * math.exp(-PL["PLANT_RR_EA_J_MOL"] / (RGAS * (self.t[i] + 273.15)))
            dx = self.x[i] * (1.0 - math.exp(-k * dt))
            self.x[i] -= dx
            heat += PL["PLANT_RR_H_J"] * dx / dt
            self.t[i] += dt * (heat - (self.t[i] - self.t_amb) / self.rth) / PL["PLANT_CTH_J_K"]
            self.vt[i], self.ic[i] = vt, ic

    def step(self, chg_on=1, dsg_on=1, bal=0):
        n = 100 if max(self.t) > PL["PLANT_HOT_C"] else 1
        for _ in range(n):
            self._substep(PL["PLANT_DT_S"] / n, chg_on, dsg_on, bal)

    def measure(self):
        cell = [math.floor(v * 1000.0 + 0.5) for v in self.vt]
        temp = [math.floor(t * 10.0 + 0.5) for t in self.t]
        return cell, math.floor(self.ip * 1000.0 + 0.5) + int(PL["PLANT_I_OFFSET_MA"]), temp


# --------------------------------------------------------------------------
# BMS: line-for-line port of src/bms/bms.c (integers only)
# --------------------------------------------------------------------------
F_OV, F_UV, F_OCC, F_OCD, F_SCD = 0x1, 0x2, 0x4, 0x8, 0x10
F_OTC, F_OTD, F_UTC, F_TSENSE = 0x20, 0x40, 0x80, 0x100
F_BLOCK_CHG = F_OV | F_OCC | F_OTC | F_OTD | F_UTC | F_TSENSE
F_BLOCK_DSG = F_UV | F_OCD | F_SCD | F_OTD | F_TSENSE
CAP_MAMS = int(CFG["BMS_CAP_MAH"]) * 3600 * 1000
C = {k: int(v) for k, v in CFG.items() if not isinstance(v, tuple)}


def soc_from_ocv_pm(mv):
    if mv <= OCV_MV[0]:
        return 0
    if mv >= OCV_MV[10]:
        return 1000
    i = 0
    while mv >= OCV_MV[i + 1]:
        i += 1
    return 100 * i + 100 * (mv - OCV_MV[i]) // (OCV_MV[i + 1] - OCV_MV[i])


class Bms:
    def __init__(self, rested_mv):
        self.faults = 0
        self.cnt = {k: 0 for k in ("ov", "uv", "occ", "ocd", "scd", "otc", "otd", "utc", "ts")}
        self.rec_dsg = self.rec_chg = self.full = self.cnt_hold = self.hold = 0
        self.bal = self.startup = 0
        self.q = CAP_MAMS * soc_from_ocv_pm(min(rested_mv)) // 1000

    def _trip(self, flag, key, cond, delay, clear):
        if cond:
            self.cnt[key] = min(self.cnt[key] + 1, 255)
            if self.cnt[key] >= delay:
                self.faults |= flag
        else:
            self.cnt[key] = 0
            if clear:
                self.faults &= ~flag

    def step(self, cell, i_ma, temp):
        vmax, vmin, tmax, tmin = max(cell), min(cell), max(temp), min(temp)
        tbad = any(t < C["BMS_TSENSE_MIN_DC"] or t > C["BMS_TSENSE_MAX_DC"] for t in temp)
        self._trip(F_TSENSE, "ts", tbad, C["BMS_TEMP_DELAY"], False)
        self._trip(F_OV, "ov", vmax >= C["BMS_OV_TRIP_MV"], C["BMS_OV_DELAY"], vmax < C["BMS_OV_CLEAR_MV"])
        self._trip(F_UV, "uv", vmin <= C["BMS_UV_TRIP_MV"], C["BMS_UV_DELAY"], vmin >= C["BMS_UV_CLEAR_MV"])
        self._trip(F_OCC, "occ", i_ma >= C["BMS_OCC_TRIP_MA"], C["BMS_OCC_DELAY"], False)
        self._trip(F_OCD, "ocd", i_ma <= -C["BMS_OCD_TRIP_MA"], C["BMS_OCD_DELAY"], False)
        self._trip(F_SCD, "scd", i_ma <= -C["BMS_SCD_TRIP_MA"], C["BMS_SCD_DELAY"], False)
        if not tbad:
            self._trip(F_OTC, "otc", tmax >= C["BMS_OTC_TRIP_DC"], C["BMS_TEMP_DELAY"], tmax < C["BMS_OTC_CLEAR_DC"])
            self._trip(F_OTD, "otd", tmax >= C["BMS_OTD_TRIP_DC"], C["BMS_TEMP_DELAY"], tmax < C["BMS_OTD_CLEAR_DC"])
            self._trip(F_UTC, "utc", tmin <= C["BMS_UTC_TRIP_DC"], C["BMS_TEMP_DELAY"], tmin >= C["BMS_UTC_CLEAR_DC"])
        if (self.faults & (F_OCD | F_SCD)) and i_ma >= C["BMS_RECOVER_MA"]:
            self.rec_dsg += 1
            if self.rec_dsg >= C["BMS_RECOVER_DELAY"]:
                self.faults &= ~(F_OCD | F_SCD)
                self.rec_dsg = 0
        else:
            self.rec_dsg = 0
        if (self.faults & F_OCC) and i_ma <= -C["BMS_RECOVER_MA"]:
            self.rec_chg += 1
            if self.rec_chg >= C["BMS_RECOVER_DELAY"]:
                self.faults &= ~F_OCC
                self.rec_chg = 0
        else:
            self.rec_chg = 0

        self.q = min(max(self.q + i_ma * C["BMS_TS_MS"], 0), CAP_MAMS)
        if vmax >= C["BMS_FULL_MV"] and C["BMS_IDLE_MA"] <= i_ma <= C["BMS_TAPER_MA"]:
            self.full += 1
            if self.full >= C["BMS_FULL_DELAY"]:
                self.q = CAP_MAMS
        else:
            self.full = 0
        if vmax >= C["BMS_CELL_FULL_MV"] and C["BMS_IDLE_MA"] <= i_ma <= C["BMS_CELL_FULL_MA"]:
            self.cnt_hold = min(self.cnt_hold + 1, 255)
            if self.cnt_hold >= C["BMS_FULL_DELAY"]:
                self.hold, self.q = 1, CAP_MAMS
        else:
            self.cnt_hold = 0
        if self.hold and vmax < C["BMS_OV_CLEAR_MV"]:
            self.hold = 0
        if self.faults & F_UV:
            self.q = 0

        if self.faults:
            st = "FAULT"
        elif i_ma > C["BMS_IDLE_MA"]:
            st = "CHARGING"
        elif i_ma < -C["BMS_IDLE_MA"]:
            st = "DISCHARGING"
        else:
            st = "STANDBY"

        if (not self.faults and st != "DISCHARGING" and vmax >= C["BMS_BAL_MIN_MV"]
                and tmax < C["BMS_BAL_MAX_DC"]):
            for i in range(N):
                d, bit = cell[i] - vmin, 1 << i
                if self.bal & bit:
                    if d <= C["BMS_BAL_STOP_MV"]:
                        self.bal &= ~bit
                elif d >= C["BMS_BAL_START_MV"]:
                    self.bal |= bit
        else:
            self.bal = 0

        ready = self.startup >= C["BMS_STARTUP_SAMPLES"]
        if not ready:
            self.startup += 1
        chg_on = int(ready and not self.hold and not (self.faults & F_BLOCK_CHG))
        dsg_on = int(ready and not (self.faults & F_BLOCK_DSG))
        return chg_on, dsg_on, self.bal, st, self.q * 1000 // CAP_MAMS


class Sim:
    """Plant + BMS in the loop, the same sequence as sim_step() in test_bms.c."""

    def __init__(self, soc0, t_amb, bms=True):
        self.p = Plant(soc0, t_amb)
        self.b = Bms(self.p.measure()[0]) if bms else None
        self.out = (1, 1, 0, "STANDBY", 0) if not bms else (0, 0, 0, "STANDBY", 0)
        self.k = 0

    def step(self):
        cell, i_ma, temp = self.p.measure()
        if self.b:
            self.out = self.b.step(cell, i_ma, temp)
        self.p.step(*self.out[:3])
        self.k += 1
        return cell


def steps(sec):
    return int(sec * 10.0 + 0.5)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def s1_charge_balance():
    """test_bms.c S1: CC/CV charge of an unbalanced pack, then 2 h on the finished charger."""
    s = Sim((0.30, 0.33, 0.29, 0.31), 25.0)
    s.p.src = "charger"
    spread0 = 1000 * (max(map(ocv, s.p.soc)) - min(map(ocv, s.p.soc)))
    vpk, bal_steps, k_done, socpk, log = 0, 0, -1, 0.0, []
    while s.k < steps(5 * 3600):
        cell = s.step()
        vpk = max(vpk, max(cell))
        bal_steps += s.out[2] != 0
        socpk = max(socpk, max(s.p.soc))
        if k_done < 0 and s.p.done:
            k_done = s.k
        if s.k % 100 == 0:
            log.append([s.k * 0.1, s.p.ip, *cell, *(100 * x for x in s.p.soc), s.out[4] / 10.0,
                        s.out[2], max(s.p.t)])
        if k_done >= 0 and s.k >= k_done + steps(7200):
            break
    spread1 = 1000 * (max(map(ocv, s.p.soc)) - min(map(ocv, s.p.soc)))
    return dict(t_chg=k_done * 0.1, vpk=vpk, spread0=spread0, spread1=spread1,
                bal_s=bal_steps * 0.1, socpk=socpk, log=np.array(log))


def overcharge(bms, t_end=2 * 3600, stop_c=None):
    """test_bms.c S3: charger without CV regulation, 3 A into a 90 % pack."""
    s = Sim((0.90,) * 4, 25.0, bms=bms)
    s.p.src, s.p.faulty = "charger", True
    vpk, trips, was, socpk, tpk, k_first, k_off, tr = 0, 0, False, 0.0, 0.0, -1, -1, []
    while s.k < steps(t_end):
        cell = s.step()
        vpk, socpk, tpk = max(vpk, max(cell)), max(socpk, max(s.p.soc)), max(tpk, max(s.p.t))
        if bms:
            ov = bool(s.b.faults & F_OV)
            trips += ov and not was
            was = ov
            if k_first < 0 and max(cell) >= C["BMS_OV_TRIP_MV"]:
                k_first = s.k
            if k_first >= 0 and k_off < 0 and not s.out[0]:
                k_off = s.k
        if s.k % 10 == 0:
            tr.append((s.k * 0.1, max(s.p.t), max(cell)))
        if stop_c is not None and tpk > stop_c:
            break
    return dict(vpk=vpk, trips=trips, socpk=socpk, tpk=tpk, t_off=k_off * 0.1, t_end=s.k * 0.1,
                trace=np.array(tr))


def critical_ambient(rth):
    """Semenov limit: the ambient at which self-heating alone just runs away. At the critical
    cell temperature Tc the heat curve touches the loss line: Q'(Tc) = 1/Rth and
    Ta = Tc - Rth Q(Tc)."""
    lo, hi = 20.0, 300.0
    ea = PL["PLANT_RR_EA_J_MOL"]
    for _ in range(80):
        tc = 0.5 * (lo + hi)
        slope = selfheat_w(tc) * ea / (RGAS * (tc + 273.15) ** 2)
        lo, hi = (tc, hi) if slope < 1.0 / rth else (lo, tc)
    return tc - rth * selfheat_w(tc), tc


def soak(t_amb, rth, hours):
    """No current, ambient held at t_amb: peak cell temperature (stops at 150 degC)."""
    p = Plant((0.5,) * 4, t_amb)
    p.rth = rth
    tpk = t_amb
    for _ in range(steps(hours * 3600)):
        p.step(0, 0, 0)
        tpk = max(tpk, p.t[0])
        if tpk > 150.0:
            break
    return tpk


def parse_spice(path):
    """TRAN / NOMINAL / CORNER lines echoed by sim/ngspice/cell_sense.cir."""
    out = {"corner": []}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "TRAN":
            out["settle_s"] = float(f[2])
        elif f[0] == "NOMINAL":
            out[f[1]] = [float(x) for x in f[2:]]
        elif f[0] == "CORNER" and len(f) == 12:
            out["corner"].append((float(f[1]), float(f[2]), [float(x) for x in f[8:12]]))
    return out


def parse_host(path):
    txt = path.read_text() if path.exists() else ""
    m = re.search(r"RESULT: (\d+)/(\d+) PASS", txt)
    met = {k: float(v) for k, v in re.findall(r"METRIC (\w+) (\S+)", txt)}
    return (int(m.group(1)), int(m.group(2))) if m else (0, -1), met


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    print("Smart BMS 04 - main.py (simulated design; no hardware built)")
    print("Limits from src/bms/bms_config.h: OV %d mV, UV %d mV, OCD %.1f A, SCD %.0f A, OT %.0f / %.0f degC,"
          " UT %.0f degC, balance start %d mV"
          % (C["BMS_OV_TRIP_MV"], C["BMS_UV_TRIP_MV"], C["BMS_OCD_TRIP_MA"] / 1e3, C["BMS_SCD_TRIP_MA"] / 1e3,
             C["BMS_OTC_TRIP_DC"] / 10, C["BMS_OTD_TRIP_DC"] / 10, C["BMS_UTC_TRIP_DC"] / 10,
             C["BMS_BAL_START_MV"]))

    print("\n[1] Constants and tables")
    need = ("BMS_OV_TRIP_MV", "BMS_UV_TRIP_MV", "BMS_SCD_TRIP_MA", "BMS_OTD_TRIP_DC", "BMS_BAL_START_MV")
    check("C01 headers parsed", all(k in CFG for k in need) and len(PL) >= 20 and N == 4,
          "%d BMS + %d plant constants" % (len(CFG), len(PL)))
    mono = all(b > a for a, b in zip(OCV_MV, OCV_MV[1:]))
    inv = all(soc_from_ocv_pm(v) == 100 * i for i, v in enumerate(OCV_MV))
    check("C02 OCV table monotonic, lookup inverts it", mono and inv and len(OCV_MV) == 11,
          "%d..%d mV, 11 points" % (OCV_MV[0], OCV_MV[-1]))
    check("C03 OV trip < OCV(100 %) + margin; UV > OCV(0 %) - margin",
          OCV_MV[10] < C["BMS_OV_TRIP_MV"] <= 4300 and 2500 <= C["BMS_UV_TRIP_MV"] < OCV_MV[0],
          "OV %d vs %d mV, UV %d vs %d mV" % (C["BMS_OV_TRIP_MV"], OCV_MV[10], C["BMS_UV_TRIP_MV"], OCV_MV[0]))

    print("\n[2] Cell model (cell 1: 3.00 Ah, R scale 1.00)")
    p = Plant((0.5,) * 4, 25.0)
    p.src, p.load_a = "load_i", 3.0
    for _ in range(steps(10)):
        p.step()
    dv_sim = ocv(0.5) - p.vt[0]
    r0, r1, r2 = PL["PLANT_R0_OHM"], PL["PLANT_R1_OHM"], PL["PLANT_R2_OHM"]
    t1, t2 = PL["PLANT_TAU1_S"], PL["PLANT_TAU2_S"]
    dq = 3.0 * 10.0 / (3.0 * 3600.0)
    # The last stored terminal voltage is from the start of the final 0.1 s step
    tl = 10.0 - PL["PLANT_DT_S"]
    dv_an = 3.0 * (r0 + r1 * (1 - math.exp(-tl / t1)) + r2 * (1 - math.exp(-tl / t2))) + (ocv(0.5) - ocv(0.5 - dq * tl / 10.0))
    check("C04 3 A, 10 s pulse: sag vs closed form", abs(dv_sim - dv_an) < 0.5e-3,
          "sim %.2f mV, analytic %.2f mV" % (dv_sim * 1e3, dv_an * 1e3))
    rt0 = r_temp(0.0)
    check("C05 resistance x2 at 0 degC", abs(rt0 - 2.0) < 0.05, "R(0 degC) / R(25 degC) = %.3f" % rt0)
    p = Plant((1.0,) * 4, 25.0)
    p.src, p.load_a = "load_i", 1.5
    ah = 0.0
    while p.vt[0] > C["BMS_UV_TRIP_MV"] / 1000.0 and ah < 4.0:
        p.step()
        ah += 1.5 * PL["PLANT_DT_S"] / 3600.0
    # The table's 0 % point is 3.00 V at rest; a 2.80 V cutoff under load reaches slightly past it
    check("C06 C/2 to 2.80 V: capacity 95-105 % of 3.00 Ah", 0.95 * 3.0 <= ah <= 1.05 * 3.0,
          "%.3f Ah, ends at %.1f %% of the table" % (ah, 100 * p.soc[0]))

    print("\n[3] Thermal runaway model (lumped Arrhenius reaction)")
    q100 = selfheat_w(100.0)
    check("C07 self-heating calibration at 100 degC", abs(q100 - PL["PLANT_RR_Q100_W"]) < 1e-9,
          "%.3f W (x2 every %.1f K)" % (q100, math.log(2) * RGAS * 373.15 ** 2 / PL["PLANT_RR_EA_J_MOL"]))
    ta15, tc15 = critical_ambient(PL["PLANT_RTH_K_W"])
    ta60, tc60 = critical_ambient(PL["PLANT_RTH_FAIL_K_W"])
    check("C08 Semenov critical ambient > 60 degC", min(ta15, ta60) > 60.0,
          "Rth 15: %.1f degC, Rth 60: %.1f degC" % (ta15, ta60))
    lo_t, hi_t = soak(ta60 - 5.0, PL["PLANT_RTH_FAIL_K_W"], 3.0), soak(ta60 + 5.0, PL["PLANT_RTH_FAIL_K_W"], 3.0)
    check("C09 Rth 60 soak: stable at Ta_crit -5 K, runaway at +5 K", lo_t < 120.0 and hi_t > 150.0,
          "peak %.1f degC / %.1f degC" % (lo_t, hi_t))

    print("\n[4] Charger without CV regulation (3 A from 90 %), with and without the BMS")
    nob = overcharge(False, t_end=3600, stop_c=150.0)
    check("C10 no BMS: overcharge drives thermal runaway", nob["tpk"] > 150.0,
          "150 degC after %.0f s, peak SoC %.1f %%" % (nob["t_end"], nob["socpk"] * 100))
    ov = overcharge(True)
    check("C11 BMS: no cell above 100 %, peak < 45 degC", ov["socpk"] <= 1.0 and ov["tpk"] < 45.0,
          "SoC %.2f %%, %.2f degC, %d OV trips" % (ov["socpk"] * 100, ov["tpk"], ov["trips"]))

    print("\n[5] Charge and balance (test_bms.c S1) and the Python port vs the C host test")
    s1 = s1_charge_balance()
    res, met = parse_host(HOST_LOG)
    check("C12 host test log RESULT all PASS", res[0] == res[1], "%d/%d" % res)
    neg = NEG_LOG.read_text() if NEG_LOG.exists() else ""
    check("C13 negative gate log: every mutant caught", "NEGATIVE GATE: 6/6 mutants caught" in neg,
          "6 mutants" if "6/6" in neg else "log missing or incomplete")
    pairs = [
        ("C14 S1 charge time", s1["t_chg"], met.get("s1_t_chg_s"), 0.15, "s"),
        ("C15 S1 peak cell voltage", s1["vpk"], met.get("s1_vpk_mv"), 0.5, "mV"),
        ("C16 S1 OCV spread after balancing", s1["spread1"], met.get("s1_spread_mv"), 0.01, "mV"),
        ("C17 S1 balancing time", s1["bal_s"], met.get("s1_bal_s"), 0.15, "s"),
        ("C18 S1 peak cell SoC", s1["socpk"] * 100, (met.get("s1_socpk") or 0) * 100, 0.001, "%"),
        ("C19 S3 first OV trip", ov["t_off"], met.get("s3_t_off_s"), 0.15, "s"),
        ("C20 S3 OV trip count", ov["trips"], met.get("s3_trips"), 0.5, ""),
        ("C21 S3 peak SoC", ov["socpk"] * 100, (met.get("s3_socpk") or 0) * 100, 0.001, "%"),
    ]
    for name, py, cv, tol, unit in pairs:
        ok = cv is not None and abs(py - cv) <= tol
        check(name + ": Python vs C", ok, "Python %.6g, C %s %s" % (py, "%.6g" % cv if cv is not None else "n/a", unit))

    print("\n[6] Cell-voltage sense front end (ngspice: sim/ngspice/cell_sense.cir)")
    sp = parse_spice(SPICE_LOG)
    deck = SPICE_DECK.read_text() if SPICE_DECK.exists() else ""
    gain_ok = "{2Meg * (1" in deck and "{1Meg * (1" in deck and "R15 amp1 vcell1 10k" in deck
    check("C22 SPICE log complete", len(sp["corner"]) == 128 and "err_V" in sp and gain_ok,
          "%d corner runs, deck gain 1M/2M = 0.5" % len(sp["corner"]))
    if len(sp["corner"]) == 128 and "err_V" in sp:
        worst = {}
        for tol, vc, errs in sp["corner"]:
            worst[tol] = max(worst.get(tol, 0.0), max(abs(e) for e in errs))
        check("C23 nominal error per channel < 0.5 mV", max(map(abs, sp["err_V"])) < 0.5e-3,
              "max %.3f mV" % (max(map(abs, sp["err_V"])) * 1e3))
        check("C24 0.01 % networks: worst case <= 10 mV", worst[1e-4] <= 10e-3,
              "%.2f mV (16 corners x Vos +/-250 uV, 2.8/4.25 V)" % (worst[1e-4] * 1e3))
        check("C25 0.1 % networks would miss 10 mV", worst[1e-3] > 10e-3,
              "%.1f mV: common mode x mismatch, top cell worst" % (worst[1e-3] * 1e3))
        ia = sp["cell_A"]
        imb = max(ia) - min(ia)
        pct_month = imb * 720.0 / 3.0 * 100.0
        check("C26 tap-load imbalance < 1 % SoC per month", pct_month < 1.0,
              "%.1f uA -> %.2f %%/month (Iq %.1f mA common)" % (imb * 1e6, pct_month, min(ia) * 1e3))
        inp4, vamp, vcell4 = sp["pins_V"]
        check("C27 op-amp inputs inside supply, output in ADC range", inp4 < vamp - 0.5 and vcell4 * 4.30 / 4.20 < 2.45,
              "IN+ %.2f V vs VAMP %.2f V; VCELL at 4.30 V = %.3f V" % (inp4, vamp, vcell4 * 4.30 / 4.20))
        check("C28 4.00 -> 4.25 V step settles within one BMS step", sp.get("settle_s", 1.0) < C["BMS_TS_MS"] / 1000.0,
              "%.1f ms to 1 mV (10k / 1 uF filter)" % (sp.get("settle_s", 1.0) * 1e3))

    print("\n[7] KiCad schematic (hw/kicad/bms.kicad_sch)")
    sch = SCHEMATIC.read_text() if SCHEMATIC.exists() else ""
    vals = dict(re.findall(r'\(property "Reference" "([^"#][^"]*)".*?\(property "Value" "([^"]*)"', sch, re.S))
    want = {}
    for n in range(1, 5):
        want.update({"R%d1" % n: "2M 0.01%", "R%d2" % n: "1M 0.01%", "R%d3" % n: "2M 0.01%",
                     "R%d4" % n: "1M 0.01%", "R%d5" % n: "10k", "C%d5" % n: "1u"})
    want.update({"R1": "100", "C1": "1u 25V", "U1": "OPA4197"})
    bad = sorted(k for k, v in want.items() if vals.get(k) != v)
    check("C29 schematic sense network = SPICE deck", not bad and len(vals) >= 80,
          "%d parts, %s" % (len(vals), "mismatch " + ",".join(bad) if bad else "24 network parts + supply match"))
    erc = ERC_LOG.read_text() if ERC_LOG.exists() else ""
    check("C30 ERC 0 errors 0 warnings, netlist check PASS",
          "Errors 0  Warnings 0" in erc and erc.rstrip().endswith("RESULT: PASS"),
          "%d nets checked" % erc.count("\nPASS "))

    write_csv(s1["log"])
    print("\n  S1: charge %.0f s, peak cell %d mV, OCV spread %.1f -> %.1f mV, balancing %.0f s"
          % (s1["t_chg"], s1["vpk"], s1["spread0"], s1["spread1"], s1["bal_s"]))
    print("  wrote %s (%d rows, one per 10 s)" % (CSV_PATH.relative_to(HERE), len(s1["log"])))

    fails = RESULTS.count(False)
    print("\nRESULT: %d/%d PASS" % (len(RESULTS) - fails, len(RESULTS)))
    if not args.no_plot:
        plot(s1, nob, ov)
    return 1 if fails else 0


def write_csv(log):
    head = (["t_s", "i_pack_a"] + ["v%d_mv" % (i + 1) for i in range(N)] + ["soc%d_pct" % (i + 1) for i in range(N)]
            + ["bms_soc_pct", "bal_mask", "tmax_c"])
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(CSV_PATH, log, delimiter=",", header=",".join(head), comments="",
               fmt=["%.1f", "%.4f"] + ["%d"] * N + ["%.3f"] * N + ["%.1f", "%d", "%.2f"])


def plot(s1, nob, ov):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    L = s1["log"]
    th = L[:, 0] / 3600.0
    fig, ax = plt.subplots(2, 2, figsize=(12, 8))
    a = ax[0, 0]
    for i in range(N):
        a.plot(th, L[:, 2 + i] / 1000.0, lw=1, label="cell %d" % (i + 1))
    a.axhline(C["BMS_OV_TRIP_MV"] / 1000.0, color="r", ls="--", lw=0.8, label="OV trip")
    a.set(title="S1 CC/CV charge, then 2 h rest: cell voltages", xlabel="time (h)", ylabel="V")
    b = a.twinx()
    b.plot(th, L[:, 1], "k:", lw=1)
    b.set_ylabel("pack current (A, dotted)")
    a.legend(fontsize=7, loc="lower right")

    a = ax[0, 1]
    for i in range(N):
        a.plot(th, L[:, 6 + i], lw=1, label="cell %d true" % (i + 1))
    a.plot(th, L[:, 10], "k--", lw=1.2, label="BMS estimate")
    a.set(title="S1 state of charge", xlabel="time (h)", ylabel="%")
    a.legend(fontsize=7)

    a = ax[1, 0]
    soc = L[:, 6:10]
    a.plot(th, soc.max(axis=1) - soc.min(axis=1), lw=1.2)
    a.set(title="S1 SoC spread (max - min) and bleed resistors", xlabel="time (h)", ylabel="SoC spread (%)")
    b = a.twinx()
    b.step(th, [bin(int(m)).count("1") for m in L[:, 11]], "g", lw=0.6, where="post")
    b.set_ylabel("cells bleeding (green)")

    a = ax[1, 1]
    tn, to = nob["trace"], ov["trace"]
    a.plot(tn[:, 0] / 60.0, tn[:, 1], "r", label="no BMS")
    a.plot(to[:, 0] / 60.0, to[:, 1], "b", label="BMS (OV trip, hysteresis)")
    a.axhline(C["BMS_OTC_TRIP_DC"] / 10.0, color="gray", ls="--", lw=0.8, label="charge OT trip")
    a.set(title="Charger without CV regulation, 3 A from 90 %", xlabel="time (min)", ylabel="hottest cell (degC)",
          xlim=(0, 60), yscale="log")
    a.legend(fontsize=7)
    fig.tight_layout()
    FIGURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE_PATH, dpi=110)
    print("  wrote %s" % FIGURE_PATH.relative_to(HERE))


if __name__ == "__main__":
    sys.exit(main())
