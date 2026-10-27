#!/usr/bin/env python3
"""Regenerate the KiCad 10 schematic for the digital synchronous buck converter (12 V -> 5 V / 3 A).

Writes, next to this script:
  sync_buck.kicad_sch   schematic (stock symbols copied verbatim from the KiCad libraries)
  sync_buck.kicad_pro   minimal project file (ERC severities as in project 01)
  sync_buck.kicad_sym   project library: UCC27211, INA181A3, generic 30 V NMOS (G=1 D=2 S=3)
  sym-lib-table         project symbol library table
  fp-lib-table          project footprint library table

Stdlib only and deterministic: every UUID is uuid5() of a fixed name, no timestamps.

  python3 gen_schematic.py            regenerate the files above
  python3 gen_schematic.py --export   also write ../../docs/img/schematic.{svg,pdf,png}

Coordinates are sheet millimetres (y grows downwards); the design uses g(n) = n x 1.27 mm.
Every connection point sits on the 1.27 mm grid; build() asserts it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "sync_buck"
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
SYMDIR = os.environ.get("KICAD_SYMBOL_DIR", KICAD_APP + "/SharedSupport/symbols")
KICAD_CLI = os.environ.get("KICAD_CLI", KICAD_APP + "/MacOS/kicad-cli")
SCH_VERSION = "20260306"      # what KiCad 10.0.7 writes (checked with `kicad-cli sch upgrade`)
SYM_VERSION = "20251024"      # stock KiCad 10 .kicad_sym version
GRID = 1.27

NS = uuid.uuid5(uuid.NAMESPACE_URL, "ee-portfolio/02-sync-buck/hw/kicad")


def uid(*parts):
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


ROOT_UUID = uid("root-sheet")


# ----------------------------------------------------------------- s-expression writer
class Q(str):
    """A string atom that is written quoted."""


class Raw:
    """Verbatim s-expression text (one block), re-indented on output."""

    def __init__(self, text):
        self.text = text


def num(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def atom(a):
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, (int, float)):
        return num(a)
    return str(a)


def ser(node, ind=0):
    tab = "\t" * ind
    if isinstance(node, Raw):
        return "\n".join(tab + line for line in node.text.split("\n"))
    head = [x for x in node if not isinstance(x, (list, Raw))]
    subs = [x for x in node if isinstance(x, (list, Raw))]
    s = tab + "(" + " ".join(atom(a) for a in head)
    if not subs:
        return s + ")"
    return s + "\n" + "\n".join(ser(x, ind + 1) for x in subs) + "\n" + tab + ")"


# ----------------------------------------------------------------- stock library access
def block_end(text, start):
    depth, i = 0, start
    while True:
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1


_lib_cache = {}


def stock_symbol(lib_id):
    """Return (Raw block renamed to lib_id, pins) copied verbatim from the stock library."""
    lib, name = lib_id.split(":")
    if lib not in _lib_cache:
        with open(os.path.join(SYMDIR, lib + ".kicad_sym"), encoding="utf-8") as f:
            _lib_cache[lib] = f.read()
    text = _lib_cache[lib]
    m = re.search(r'\n(\t)\(symbol "%s"\n' % re.escape(name), text)
    if not m:
        raise SystemExit("symbol %s not found in %s" % (name, lib))
    start = m.start(1) + 1
    block = text[start:block_end(text, start) + 1]
    if "(extends " in block:
        raise SystemExit("%s is a derived symbol; flattening is not implemented" % lib_id)
    lines = block.split("\n")
    lines = [lines[0].replace('(symbol "%s"' % name, '(symbol "%s"' % lib_id, 1)] + [
        l[1:] if l.startswith("\t") else l for l in lines[1:]]
    raw = "\n".join(lines)
    if lib_id == "Device:R_Shunt":      # R6 only: hide pin numbers (they overlap when rotated)
        raw = raw.replace('\n\t(pin_names', '\n\t(pin_numbers\n\t\t(hide yes)\n\t)\n\t(pin_names', 1)
    return Raw(raw), parse_pins(block)


def parse_pins(block):
    pins = {}
    for m in re.finditer(r"\(pin (\w+) (\w+)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", block):
        body = block[m.start():block_end(block, m.start()) + 1]
        name = re.search(r'\(name "((?:[^"\\]|\\.)*)"', body).group(1)
        number = re.search(r'\(number "((?:[^"\\]|\\.)*)"', body).group(1)
        pins[number] = dict(type=m.group(1), x=float(m.group(3)), y=float(m.group(4)), name=name)
    return pins




# ----------------------------------------------------------------- custom symbols
def font(size=1.27, bold=False):
    f = ["font", ["size", size, size]]
    if bold:
        f.append(["bold", "yes"])
    return f


def lib_prop(name, value, x, y, ang=0, hide=False):
    p = ["property", Q(name), Q(value), ["at", x, y, ang], ["show_name", "no"],
         ["do_not_autoplace", "no"]]
    if hide:
        p.append(["hide", "yes"])
    p.append(["effects", font()])
    return p


# Pin definitions: number -> (pin name, type, side, slot). Slots are in 1.27 mm units from the
# symbol centre: left/right pins use y (up positive), top/bottom pins use x.
CUSTOM = {
    "UCC27211": dict(
        ref="U", value="UCC27211", hw=6, hh=8, desc="120 V half-bridge gate driver, 4 A, SOIC-8 "
        "(TI UCC27211): 1 VDD, 2 HB, 3 HO, 4 HS, 5 HI, 6 LI, 7 VSS, 8 LO",
        ds="https://www.ti.com/lit/ds/symlink/ucc27211.pdf", kw="gate driver half bridge",
        pins={"1": ("VDD", "power_in", "L", 6), "5": ("HI", "input", "L", 2),
              "6": ("LI", "input", "L", -2), "7": ("VSS", "power_in", "L", -6),
              "2": ("HB", "passive", "R", 6), "3": ("HO", "output", "R", 2),
              "4": ("HS", "passive", "R", -2), "8": ("LO", "output", "R", -6)}),
    "INA181A3": dict(
        ref="U", value="INA181A3", hw=6, hh=6, desc="Current-sense amplifier, gain 100 V/V "
        "(TI INA181A3), SOT-23-6: 1 OUT, 2 GND, 3 IN+, 4 IN-, 5 REF, 6 VS",
        ds="https://www.ti.com/lit/ds/symlink/ina181.pdf", kw="current sense amplifier",
        pins={"3": ("IN+", "input", "L", 4), "4": ("IN-", "input", "L", 0),
              "5": ("REF", "input", "L", -4), "1": ("OUT", "output", "R", 0),
              "6": ("VS", "power_in", "T", 0), "2": ("GND", "power_in", "B", 0)}),
    "NMOS_30V_GDS": dict(
        ref="Q", value="NMOS_30V_GDS", hw=4, hh=3, desc="Generic 30 V N-channel MOSFET. "
        "PIN ORDER: 1 = G (gate), 2 = D (drain), 3 = S (source) (DPAK TO-252 pad order)",
        ds="", kw="nmos mosfet generic 30V",
        pins={"1": ("G", "passive", "L", 0), "2": ("D", "passive", "T", 0),
              "3": ("S", "passive", "B", 0)}),
}
PIN_LEN = 2.54


def pin_xy(c, side, slot):
    """Library coordinates (mm, y up) and pin angle of one custom pin connection point."""
    hw, hh = c["hw"] * GRID, c["hh"] * GRID
    s = slot * GRID
    if side == "L":
        return -(hw + PIN_LEN), s, 0
    if side == "R":
        return hw + PIN_LEN, s, 180
    if side == "T":
        return s, hh + PIN_LEN, 270
    return s, -(hh + PIN_LEN), 90


def custom_symbol(sym_name, c):
    hw, hh = c["hw"] * GRID, c["hh"] * GRID
    gfx = [["rectangle", ["start", -hw, hh], ["end", hw, -hh],
            ["stroke", ["width", 0.254], ["type", "default"]], ["fill", ["type", "background"]]]]
    pins = []
    for number, (name, ptype, side, slot) in sorted(c["pins"].items()):
        x, y, ang = pin_xy(c, side, slot)
        pins.append(["pin", ptype, "line", ["at", x, y, ang], ["length", PIN_LEN],
                     ["name", Q(name), ["effects", font()]],
                     ["number", Q(number), ["effects", font()]]])
    return ["symbol", Q(sym_name),
            ["pin_names", ["offset", 1.016]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            ["in_pos_files", "yes"], ["duplicate_pin_numbers_are_jumpers", "no"],
            lib_prop("Reference", c["ref"], 0, hh + PIN_LEN + 1.27),
            lib_prop("Value", c["value"], 0, -(hh + PIN_LEN + 1.27)),
            lib_prop("Footprint", "", 0, 0, hide=True),
            lib_prop("Datasheet", c["ds"], 0, 0, hide=True),
            lib_prop("Description", c["desc"], 0, 0, hide=True),
            lib_prop("ki_keywords", c["kw"], 0, 0, hide=True),
            ["symbol", Q(sym_name.split(":")[-1] + "_0_1")] + gfx,
            ["symbol", Q(sym_name.split(":")[-1] + "_1_1")] + pins,
            ["embedded_fonts", "no"]]


def custom_pins(c):
    out = {}
    for number, (name, ptype, side, slot) in c["pins"].items():
        x, y, _ = pin_xy(c, side, slot)
        out[number] = dict(type=ptype, x=x, y=y, name=name)
    return out


CUSTOM_IDS = {PROJECT + ":" + k: k for k in CUSTOM}


# ----------------------------------------------------------------- schematic builder
def on_grid(v):
    return abs(v / GRID - round(v / GRID)) < 1e-6


def snap(v):
    return round(v, 4)


class Sheet:
    def __init__(self):
        self.lib = {}            # lib_id -> lib_symbols entry
        self.lib_pins = {}       # lib_id -> pins
        self.symbols, self.wires, self.labels, self.texts, self.ncs = [], [], [], [], []
        self.pin_at = {}         # (ref, number) -> (x, y)
        self.points = []         # every connection point: (x, y, what)
        self.pwr_n = 0
        self.flg_n = 0

    def need(self, lib_id):
        if lib_id in self.lib:
            return
        if lib_id in CUSTOM_IDS:
            c = CUSTOM[CUSTOM_IDS[lib_id]]
            self.lib[lib_id] = custom_symbol(lib_id, c)
            self.lib_pins[lib_id] = custom_pins(c)
        else:
            self.lib[lib_id], self.lib_pins[lib_id] = stock_symbol(lib_id)

    @staticmethod
    def xform(px, py, rot, mirror):
        x, y = px, -py                     # library y-up -> sheet y-down
        x, y = {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[rot]
        if mirror == "x":
            y = -y
        elif mirror == "y":
            x = -x
        return x, y

    def place(self, ref, lib_id, value, x, y, rot=0, mirror=None, footprint="", fields=None,
              power=False):
        """fields: {"Reference": (x, y, justify), "Value": (...)} in sheet coordinates.
        Rotated symbols get field angle 90 so the text still reads horizontally."""
        assert not (rot and mirror), "rotation and mirror together are not used here"
        self.need(lib_id)
        pins = self.lib_pins[lib_id]
        fang = 90 if rot in (90, 270) else 0
        fields = fields or {}
        su = uid("sym", ref)
        lib_text = self.lib[lib_id]
        desc = ""
        ds = ""
        if isinstance(lib_text, Raw):
            m = re.search(r'\(property "Description" "((?:[^"\\]|\\.)*)"', lib_text.text)
            desc = m.group(1).replace('\\"', '"') if m else ""
            m = re.search(r'\(property "Datasheet" "((?:[^"\\]|\\.)*)"', lib_text.text)
            ds = m.group(1) if m else ""
        else:
            ds = CUSTOM[CUSTOM_IDS[lib_id]]["ds"]
            desc = [p for p in lib_text if isinstance(p, list) and p[:2] == ["property", "Description"]]
            desc = desc[0][2] if desc else ""

        def fprop(name, val, default_xy, hide=False):
            fx, fy, just = fields.get(name, (default_xy[0], default_xy[1], None))
            return ["property", Q(name), Q(val), ["at", fx, fy, fang]] + (
                [["hide", "yes"]] if hide else []) + [
                ["show_name", "no"], ["do_not_autoplace", "no"],
                ["effects", font()] + ([["justify"] + just.split()] if just else [])]

        props = [
            fprop("Reference", ref, (x, y), hide=power),
            fprop("Value", value, (x, y)),
            fprop("Footprint", footprint, (x, y), hide=True),
            fprop("Datasheet", ds, (x, y), hide=True),
            fprop("Description", desc, (x, y), hide=True),
        ]
        sym = ["symbol", ["lib_id", Q(lib_id)], ["at", x, y, rot]]
        if mirror:
            sym.append(["mirror", mirror])
        sym += [["unit", 1], ["body_style", 1], ["exclude_from_sim", "no"],
                ["in_bom", "no" if power else "yes"], ["on_board", "no" if power else "yes"],
                ["in_pos_files", "no" if power else "yes"], ["dnp", "no"], ["uuid", Q(su)]]
        sym += props
        for number in sorted(pins, key=lambda n: (len(n), n)):
            sym.append(["pin", Q(number), ["uuid", Q(uid("pin", ref, number))]])
        sym.append(["instances", ["project", Q(PROJECT),
                                  ["path", Q("/" + ROOT_UUID), ["reference", Q(ref)], ["unit", 1]]]])
        self.symbols.append(sym)
        out = {}
        for number, p in pins.items():
            dx, dy = self.xform(p["x"], p["y"], rot, mirror)
            pt = (snap(x + dx), snap(y + dy))
            out[number] = pt
            self.pin_at[(ref, number)] = pt
            self.points.append((pt[0], pt[1], "pin %s.%s" % (ref, number)))
        return out

    def gnd(self, x, y):
        self.pwr_n += 1
        self.place("#PWR%02d" % self.pwr_n, "power:GND", "GND", x, y, power=True,
                   fields={"Reference": (x, y + 6.35, None), "Value": (x, y + 3.81, None)})

    def v33(self, x, y):
        self.pwr_n += 1
        self.place("#PWR%02d" % self.pwr_n, "power:+3V3", "+3V3", x, y, power=True,
                   fields={"Reference": (x, y + 3.81, None), "Value": (x, y - 3.556, None)})

    def pwr_flag(self, x, y):
        self.flg_n += 1
        self.place("#FLG%02d" % self.flg_n, "power:PWR_FLAG", "PWR_FLAG", x, y, power=True,
                   fields={"Reference": (x, y - 1.905, None), "Value": (x, y - 3.81, None)})

    def wire(self, *pts):
        for a, b in zip(pts, pts[1:]):
            a, b = (snap(a[0]), snap(a[1])), (snap(b[0]), snap(b[1]))
            assert a != b and (a[0] == b[0] or a[1] == b[1]), (a, b)
            self.wires.append((a, b))

    def label(self, name, x, y, left=False):
        self.labels.append((name, snap(x), snap(y), left))

    def nc(self, x, y):
        self.ncs.append((snap(x), snap(y)))

    def text(self, s, x, y, size=1.27, bold=False, justify="left top"):
        self.texts.append((s, x, y, size, bold, justify))

    def junctions(self):
        count = {}
        for a, b in self.wires:
            for p in (a, b):
                count[p] = count.get(p, 0) + 1
        for (x, y, _what) in self.points:
            if (x, y) in count:
                count[(x, y)] += 1
        return sorted(p for p, n in count.items() if n >= 3)

    def check(self):
        bad = [p for p in self.points if not (on_grid(p[0]) and on_grid(p[1]))]
        bad += [(x, y, "wire") for w in self.wires for (x, y) in w if not (on_grid(x) and on_grid(y))]
        bad += [(x, y, "label " + n) for n, x, y, _ in self.labels if not (on_grid(x) and on_grid(y))]
        bad += [(x, y, "no_connect") for x, y in self.ncs if not (on_grid(x) and on_grid(y))]
        if bad:
            raise SystemExit("off-grid connection points: %r" % bad)
        # every label must sit on a wire (end point or on the segment)
        for n, x, y, _ in self.labels:
            if not any(min(a[0], b[0]) <= x <= max(a[0], b[0]) and
                       min(a[1], b[1]) <= y <= max(a[1], b[1]) for a, b in self.wires):
                raise SystemExit("label %s at (%s, %s) is not on a wire" % (n, x, y))

    def sexpr(self, title_block):
        top = ["kicad_sch", ["version", SCH_VERSION], ["generator", Q("gen_schematic.py")],
               ["generator_version", Q("10.0")], ["uuid", Q(ROOT_UUID)], ["paper", Q("A3")],
               title_block]
        top.append(["lib_symbols"] + [self.lib[k] for k in sorted(self.lib)])
        for (x, y) in self.junctions():
            top.append(["junction", ["at", x, y], ["diameter", 0], ["color", 0, 0, 0, 0],
                        ["uuid", Q(uid("junction", x, y))]])
        for (x, y) in self.ncs:
            top.append(["no_connect", ["at", x, y], ["uuid", Q(uid("nc", x, y))]])
        for (a, b) in self.wires:
            top.append(["wire", ["pts", ["xy", a[0], a[1]], ["xy", b[0], b[1]]],
                        ["stroke", ["width", 0], ["type", "default"]],
                        ["uuid", Q(uid("wire", a, b))]])
        for (n, x, y, left) in self.labels:
            top.append(["label", Q(n), ["at", x, y, 180 if left else 0],
                        ["effects", font(), ["justify", "right" if left else "left", "bottom"]],
                        ["uuid", Q(uid("label", n, x, y))]])
        for i, (s, x, y, size, bold, just) in enumerate(self.texts):
            top.append(["text", Q(s), ["exclude_from_sim", "no"], ["at", x, y, 0],
                        ["effects", font(size, bold), ["justify"] + just.split()],
                        ["uuid", Q(uid("text", i))]])
        top += self.symbols
        top.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
        return ser(top) + "\n"



# ----------------------------------------------------------------- the design
def g(n):
    """Grid units -> millimetres (1 unit = 1.27 mm)."""
    return round(n * GRID, 4)


def G(x, y):
    return g(x), g(y)


def F(ref, val, just=None):
    """Field positions given in grid units: ref = (x, y), val = (x, y)."""
    return {"Reference": (g(ref[0]), g(ref[1]), just), "Value": (g(val[0]), g(val[1]), just)}


FP_R = "Resistor_SMD:R_0603_1608Metric"
FP_C = "Capacitor_SMD:C_0603_1608Metric"
FP_C1210 = "Capacitor_SMD:C_1210_3225Metric"
FP_C0805 = "Capacitor_SMD:C_0805_2012Metric"
FP_CELEC = "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm"
FP_CPOLY = "Capacitor_SMD:CP_Elec_6.3x7.7"
FP_L = "Inductor_SMD:L_Bourns_SRP1245A"
FP_SHUNT = "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm"   # 4-pad Kelvin 2512
FP_HDR2 = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
FP_HDR7 = "Connector_PinHeader_2.54mm:PinHeader_1x07_P2.54mm_Vertical"
FP_SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
FP_SOT236 = "Package_TO_SOT_SMD:SOT-23-6"
FP_DPAK = "Package_TO_SOT_SMD:TO-252-3_TabPin2"        # pad 1 G, pad 2 D (tab), pad 3 S

LIB_Q = PROJECT + ":NMOS_30V_GDS"
LIB_U1 = PROJECT + ":UCC27211"
LIB_U2 = PROJECT + ":INA181A3"
CONN2, CONN7 = "Connector_Generic:Conn_01x02", "Connector_Generic:Conn_01x07"

NOTES = "\n".join([
    "DESIGN NOTES (simulated design, nothing built)",
    "Buck: VIN 12 V -> VOUT 5 V / 3 A, ideal duty D = 5 / 12 = 0.417",
    "Q1 / Q2: generic 30 V NMOS, pin order G = 1, D = 2, S = 3 (DPAK TO-252 pad order)",
    "U1 UCC27211: VDD fed from VIN, C4 1 uF decoupling; R1 / R2 2.2 Ω gate resistors; R3 / R4 10k pulldowns",
    "Snubber SW -> R5 1.8 Ω -> SNB -> C6 1 nF C0G -> GND",
    "R6 shunt: 5 mΩ 1 %, 4-terminal Kelvin (force pins 1 / 4, sense pins 2 / 3)",
    "  V = 3 A x 5 mΩ = 15 mV; P = (3 A)² x 5 mΩ = 45 mW",
    "U2 INA181A3 (gain 100 V/V), REF = GND, VS = +3V3: ISENSE = 100 x 15 mV = 1.5 V at 3 A",
    "  R9 1k + C11 1 nF anti-alias: fc = 1 / (2π x 1k x 1 nF) = 159 kHz",
    "VSENSE divider: 5 V x 6.8k / (10k + 6.8k) = 2.02 V; R7 || R8 = 4.05k with C10 1 nF: fc = 39 kHz",
    "R10 / R11 10k pulldowns keep PWM_H / PWM_L low (FETs off) while the ESP32 is in reset",
    "VINSENSE divider: 12 V x 10k / (47k + 10k) = 2.11 V; R12 || R13 = 8.25k with C12 1 nF: fc = 19 kHz",
    "J3 ESP32 DevKit: 1 +3V3, 2 GND, 3 PWM_H (GPIO18), 4 PWM_L (GPIO19),",
    "  5 VSENSE (GPIO34, ADC1_CH6), 6 ISENSE (GPIO35, ADC1_CH7), 7 VINSENSE (GPIO33, ADC1_CH5)",
])


def build():
    s = Sheet()
    title = lambda t, x, y: s.text(t, g(x), g(y), size=1.524, bold=True, justify="left bottom")

    def stub(pt, dx, dy, name, left=False):
        """Wire from a pin to a point dx, dy grid units away, with a net label at the far end."""
        e = (pt[0] + g(dx), pt[1] + g(dy))
        s.wire(pt, e)
        s.label(name, e[0], e[1], left=left)

    def gnd_at(pt):
        s.gnd(*pt)

    # ---------------- input: J1, bulk + ceramic caps ---------------------------------
    title("INPUT 12 V", 14, 45)
    j1 = s.place("J1", CONN2, "VIN 12V", *G(18, 50), mirror="y", footprint=FP_HDR2,
                 fields=F((18, 47), (18, 55.5)))
    s.gnd(*G(22, 62))
    s.wire(j1["2"], G(22, 62))
    s.pwr_flag(*G(22, 62))                     # GND has only power-input pins otherwise
    caps = [("C1", "Device:C", "10uF 25V X7R", FP_C1210, 30),
            ("C2", "Device:C", "10uF 25V X7R", FP_C1210, 46),
            ("C3", "Device:C_Polarized", "100uF 25V electrolytic", FP_CELEC, 62)]
    rail = [j1["1"], G(26, 50)]
    s.pwr_flag(*G(26, 50))                     # VIN has only passives + power inputs otherwise
    for ref, lib, val, fp, x in caps:
        c = s.place(ref, lib, val, *G(x, 53), footprint=fp,
                    fields=F((x + 1.5, 52), (x + 1.5, 54), "left"))
        s.gnd(*c["2"])
        rail.append(c["1"])
    title("VIN SENSE", 30, 66)
    r12 = s.place("R12", "Device:R", "47k", *G(44, 70), footprint=FP_R,
                  fields=F((45.5, 69), (45.5, 71), "left"))
    r13 = s.place("R13", "Device:R", "10k", *G(44, 78), footprint=FP_R,
                  fields=F((45.5, 77), (45.5, 79), "left"))
    c12 = s.place("C12", "Device:C", "1nF", *G(52, 77), footprint=FP_C,
                  fields=F((53.5, 76), (53.5, 78), "left"))
    stub(r12["1"], 0, -2, "VIN")
    s.wire(r12["2"], G(44, 74), r13["1"])
    s.wire(G(44, 74), c12["1"], G(58, 74))
    s.label("VINSENSE", *G(58, 74))
    s.gnd(*r13["2"])
    s.gnd(*c12["2"])
    q1_d = G(98, 50)
    s.wire(*rail, G(80, 50), q1_d)
    s.label("VIN", *G(80, 50))

    # ---------------- half bridge: Q1, Q2, snubber -----------------------------------
    title("HALF BRIDGE", 76, 45)
    q1 = s.place("Q1", LIB_Q, "30V 7mOhm NMOS (generic)", *G(98, 55), footprint=FP_DPAK,
                 fields=F((102, 52), (102, 54), "left"))
    q2 = s.place("Q2", LIB_Q, "30V 7mOhm NMOS (generic)", *G(98, 71), footprint=FP_DPAK,
                 fields=F((98, 81.5), (98, 83.5)))
    s.wire(q1["3"], G(98, 63), q2["2"])
    s.gnd(*q2["3"])
    # gate pulldowns
    r3 = s.place("R3", "Device:R", "10k", *G(86, 58), footprint=FP_R,
                 fields=F((84.5, 57), (84.5, 59), "right"))
    s.wire(q1["1"], r3["1"])
    s.label("GH", *G(90, 55), left=True)
    stub(r3["2"], 0, 2, "SW")
    r4 = s.place("R4", "Device:R", "10k", *G(86, 74), footprint=FP_R,
                 fields=F((84.5, 73), (84.5, 75), "right"))
    s.wire(q2["1"], r4["1"])
    s.label("GL", *G(90, 71), left=True)
    s.gnd(*r4["2"])
    # snubber SW - R5 - SNB - C6 - GND
    r5 = s.place("R5", "Device:R", "1.8R", *G(112, 66), footprint=FP_R,
                 fields=F((113.5, 65), (113.5, 67), "left"))
    c6 = s.place("C6", "Device:C", "1nF 50V C0G", *G(112, 75), footprint=FP_C,
                 fields=F((113.5, 74), (113.5, 76), "left"))
    s.wire(r5["2"], c6["1"])
    s.label("SNB", *G(112, 70))
    s.gnd(*c6["2"])

    # ---------------- LC filter, shunt, output ----------------------------------------
    title("LC FILTER", 112, 45)
    title("SHUNT", 160, 45)
    title("OUTPUT 5 V / 3 A", 178, 45)
    l1 = s.place("L1", "Device:L", "15uH Isat>=6A DCR 20mOhm", *G(118, 63), rot=90,
                 footprint=FP_L, fields=F((118, 57), (118, 59)))
    s.wire(G(98, 63), r5["1"], l1["1"])
    s.label("SW", *G(102, 63))
    c7 = s.place("C7", "Device:C_Polarized", "470uF 6.3V polymer", *G(136, 66),
                 footprint=FP_CPOLY, fields=F((134.5, 65), (134.5, 67), "right"))
    c8 = s.place("C8", "Device:C", "10uF 10V X7R", *G(148, 66), footprint=FP_C0805,
                 fields=F((149.5, 65), (149.5, 67), "left"))
    r6 = s.place("R6", "Device:R_Shunt", "5mOhm 1%", *G(168, 63), rot=90, footprint=FP_SHUNT,
                 fields=F((168, 66.5), (168, 68.5)))
    s.wire(l1["2"], c7["1"], c8["1"], r6["1"])
    s.label("VOUT", *G(154, 63))
    s.gnd(*c7["2"])
    s.gnd(*c8["2"])
    stub(r6["2"], 0, -4, "SH_P", left=True)
    stub(r6["3"], 0, -4, "SH_N")
    j2 = s.place("J2", CONN2, "VOUT 5V", *G(188, 63), footprint=FP_HDR2,
                 fields=F((188, 59.5), (188, 68)))
    s.wire(r6["4"], j2["1"])
    s.label("VLOAD", *G(176, 63))
    s.wire(j2["2"], G(180, 65))
    s.gnd(*G(180, 65))

    # ---------------- gate driver (below the bridge) ----------------------------------
    title("GATE DRIVER", 56, 86)
    u1 = s.place("U1", LIB_U1, "UCC27211", *G(96, 100), footprint=FP_SOIC8,
                 fields=F((96, 90), (96, 110.5)))
    c4 = s.place("C4", "Device:C", "1uF 25V", *G(66, 97), footprint=FP_C,
                 fields=F((64.5, 96), (64.5, 98), "right"))
    s.wire(u1["1"], c4["1"])                              # VDD from VIN
    s.label("VIN", *G(72, 94))
    s.gnd(*c4["2"])
    stub(u1["5"], -6, 0, "PWM_H", left=True)
    stub(u1["6"], -6, 0, "PWM_L", left=True)
    s.wire(u1["7"], G(84, 106))
    s.gnd(*G(84, 106))
    stub(u1["2"], 4, 0, "HB")
    r1 = s.place("R1", "Device:R", "2.2R", *G(111, 98), rot=90, footprint=FP_R,
                 fields=F((111, 96), (111, 100)))
    r2 = s.place("R2", "Device:R", "2.2R", *G(111, 106), rot=90, footprint=FP_R,
                 fields=F((111, 104), (111, 108)))
    s.wire(u1["3"], r1["1"])
    s.label("HO", *G(105, 98))
    s.wire(r1["2"], G(118, 98))
    s.label("GH", *G(118, 98))
    s.wire(u1["8"], r2["1"])
    s.label("LO", *G(105, 106))
    s.wire(r2["2"], G(118, 106))
    s.label("GL", *G(118, 106))
    stub(u1["4"], 11, 0, "SW")
    c5 = s.place("C5", "Device:C", "100nF 50V", *G(126, 98), footprint=FP_C,
                 fields=F((127.5, 97), (127.5, 99), "left"))
    stub(c5["1"], 0, -3, "HB")
    stub(c5["2"], 0, 3, "SW")

    # ---------------- MCU header (bottom left) -----------------------------------------
    title("MCU HEADER", 8, 133)
    title("RESET PULLDOWNS", 46, 133)
    j3 = s.place("J3", CONN7, "ESP32 DevKit", *G(14, 146), mirror="y", footprint=FP_HDR7,
                 fields=F((14, 136), (14, 155)))
    s.wire(j3["1"], G(22, 140))
    s.v33(*G(22, 140))
    s.wire(j3["2"], G(38, 142))
    s.gnd(*G(38, 142))
    for n, name in (("3", "PWM_H"), ("4", "PWM_L"), ("5", "VSENSE"), ("6", "ISENSE"),
                    ("7", "VINSENSE")):
        stub(j3[n], 4, 0, name)
    for ref, x, name in (("R10", 50, "PWM_H"), ("R11", 60, "PWM_L")):
        r = s.place(ref, "Device:R", "10k", *G(x, 142), footprint=FP_R,
                    fields=F((x + 1.5, 141), (x + 1.5, 143), "left"))
        stub(r["1"], 0, -3, name)
        s.gnd(*r["2"])
    s.text("J3 pin map: 3 PWM_H = GPIO18, 4 PWM_L = GPIO19,\n5 VSENSE = GPIO34 (ADC1_CH6), "
           "6 ISENSE = GPIO35 (ADC1_CH7),\n7 VINSENSE = GPIO33 (ADC1_CH5)", g(8), g(158), size=1.0, justify="left top")

    # ---------------- current sense: U2 INA181A3 ----------------------------------------
    title("CURRENT SENSE (INA181A3, GAIN 100)", 94, 133)
    u2 = s.place("U2", LIB_U2, "INA181A3", *G(110, 150), footprint=FP_SOT236,
                 fields=F((118, 153.5), (118, 155.5), "left"))
    stub(u2["3"], -4, 0, "SH_P", left=True)
    stub(u2["4"], -4, 0, "SH_N", left=True)
    s.wire(u2["5"], G(98, 154))
    s.gnd(*G(98, 154))
    s.gnd(*u2["2"])
    s.wire(u2["6"], G(110, 138), G(116, 138), G(122, 138))
    s.pwr_flag(*G(116, 138))                              # +3V3 has no output-type source
    s.v33(*G(110, 138))
    c9 = s.place("C9", "Device:C", "100nF", *G(122, 141), footprint=FP_C,
                 fields=F((123.5, 140), (123.5, 142), "left"))
    s.gnd(*c9["2"])
    r9 = s.place("R9", "Device:R", "1k", *G(131, 150), rot=90, footprint=FP_R,
                 fields=F((131, 147.5), (131, 152.5)))
    s.wire(u2["1"], r9["1"])
    s.label("IAMP", *G(121, 150))
    c11 = s.place("C11", "Device:C", "1nF", *G(140, 153), footprint=FP_C,
                  fields=F((141.5, 152), (141.5, 154), "left"))
    s.wire(r9["2"], c11["1"], G(146, 150))
    s.label("ISENSE", *G(146, 150))
    s.gnd(*c11["2"])

    # ---------------- output voltage sense ------------------------------------------------
    title("OUTPUT VOLTAGE SENSE (DIVIDER + ANTI-ALIAS)", 166, 138)
    r7 = s.place("R7", "Device:R", "10k", *G(176, 148), footprint=FP_R,
                 fields=F((177.5, 147), (177.5, 149), "left"))
    r8 = s.place("R8", "Device:R", "6.8k", *G(176, 157), footprint=FP_R,
                 fields=F((177.5, 156), (177.5, 158), "left"))
    c10 = s.place("C10", "Device:C", "1nF", *G(184, 155), footprint=FP_C,
                  fields=F((185.5, 154), (185.5, 156), "left"))
    stub(r7["1"], 0, -3, "VLOAD")
    s.wire(r7["2"], G(176, 152), r8["1"])
    s.wire(G(176, 152), c10["1"], G(190, 152))
    s.label("VSENSE", *G(190, 152))
    s.gnd(*r8["2"])
    s.gnd(*c10["2"])

    s.text(NOTES, g(205), g(50), size=1.27, justify="left top")

    s.check()
    tb = ["title_block",
          ["title", Q("02 - Sync buck 12 V to 5 V / 3 A (simulated)")],
          ["date", Q("2026-10-27")],
          ["rev", Q("A")],
          ["company", Q("Shon Zinger")],
          ["comment", 1, Q("Simulated design only: nothing built in hardware")],
          ["comment", 2, Q("U1 UCC27211, U2 INA181A3 (gain 100), 5 mOhm Kelvin shunt, ESP32 DevKit header")]]
    return s.sexpr(tb)


def project_json():
    ignore = ["single_global_label", "four_way_junction", "simulation_model_issue",
              "footprint_filter"]
    return json.dumps({
        "board": {"design_settings": {}},
        "boards": [],
        "erc": {"rule_severities": {k: "ignore" for k in ignore}},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": PROJECT + ".kicad_pro", "version": 1},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [[ROOT_UUID, "Root"]],
        "text_variables": {},
    }, indent=2) + "\n"


def library_text():
    lib = ["kicad_symbol_lib", ["version", SYM_VERSION], ["generator", Q("gen_schematic.py")],
           ["generator_version", Q("10.0")]] + [custom_symbol(k, c) for k, c in CUSTOM.items()]
    return ser(lib) + "\n"


STOCK_SYMBOL_LIBS = ("Connector_Generic", "Device", "power")
STOCK_FOOTPRINT_LIBS = ("Capacitor_SMD", "Capacitor_THT", "Connector_PinHeader_2.54mm",
                        "Inductor_SMD", "Package_SO", "Package_TO_SOT_SMD", "Resistor_SMD")


def lib_table(kind, entries):
    rows = ['\t(lib (name "%s")(type "KiCad")(uri "%s")(options "")(descr "%s"))' % e for e in entries]
    return "(%s_lib_table\n\t(version 7)\n%s\n)\n" % (kind, "\n".join(rows))


# Project-level tables so ERC can resolve every library without a global KiCad setup.
SYM_LIB_TABLE = lib_table("sym", [(n, "${KICAD10_SYMBOL_DIR}/%s.kicad_sym" % n, "KiCad stock")
                                  for n in STOCK_SYMBOL_LIBS] +
                          [(PROJECT, "${KIPRJMOD}/%s.kicad_sym" % PROJECT,
                            "Project symbols: UCC27211, INA181A3, generic NMOS")])
FP_LIB_TABLE = lib_table("fp", [(n, "${KICAD10_FOOTPRINT_DIR}/%s.pretty" % n, "KiCad stock")
                                for n in STOCK_FOOTPRINT_LIBS])


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def export():
    img = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "img"))
    os.makedirs(img, exist_ok=True)
    sch = os.path.join(HERE, PROJECT + ".kicad_sch")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([KICAD_CLI, "sch", "export", "svg", "-o", tmp, sch], check=True)
        svgs = [f for f in os.listdir(tmp) if f.endswith(".svg")]
        assert len(svgs) == 1, svgs
        shutil.copyfile(os.path.join(tmp, svgs[0]), os.path.join(img, "schematic.svg"))
    pdf = os.path.join(img, "schematic.pdf")
    subprocess.run([KICAD_CLI, "sch", "export", "pdf", "-o", pdf, sch], check=True)
    # QuickLook renders the PDF vectors at the target size (sips would upscale a 72 dpi
    # raster and blur the text).
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["qlmanage", "-t", "-s", "3000", "-o", tmp, pdf], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.move(os.path.join(tmp, "schematic.pdf.png"), os.path.join(img, "schematic.png"))
    print("exported", img)


def main():
    write(os.path.join(HERE, PROJECT + ".kicad_sch"), build())
    write(os.path.join(HERE, PROJECT + ".kicad_pro"), project_json())
    write(os.path.join(HERE, PROJECT + ".kicad_sym"), library_text())
    write(os.path.join(HERE, "sym-lib-table"), SYM_LIB_TABLE)
    write(os.path.join(HERE, "fp-lib-table"), FP_LIB_TABLE)
    print("wrote", PROJECT + ".kicad_sch")
    if "--export" in sys.argv[1:]:
        export()


if __name__ == "__main__":
    main()
