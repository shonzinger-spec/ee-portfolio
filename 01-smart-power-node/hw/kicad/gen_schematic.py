#!/usr/bin/env python3
"""Regenerate the KiCad 10 schematic for the Smart Power-Monitoring IoT Node.

Writes, next to this script:
  power_node.kicad_sch   schematic (symbols copied verbatim from the stock KiCad libraries)
  power_node.kicad_pro   minimal project file
  power_node.kicad_sym   project library holding the custom ESP32-DevKitC-V4 symbol
  sym-lib-table          project symbol library table (stock libs + power_node.kicad_sym)
  fp-lib-table           project footprint library table (stock libs used by the footprints)

Stdlib only and deterministic: every UUID is uuid5() of a fixed name, no timestamps.

  python3 gen_schematic.py            regenerate the files above
  python3 gen_schematic.py --export   also write ../../docs/img/schematic.{svg,pdf,png}

Coordinates are sheet millimetres (y grows downwards). Every connection point sits on
the 1.27 mm grid; build() asserts it.
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
PROJECT = "power_node"
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
SYMDIR = os.environ.get("KICAD_SYMBOL_DIR", KICAD_APP + "/SharedSupport/symbols")
KICAD_CLI = os.environ.get("KICAD_CLI", KICAD_APP + "/MacOS/kicad-cli")
SCH_VERSION = "20260306"      # what KiCad 10.0.7 writes (checked with `kicad-cli sch upgrade`)
SYM_VERSION = "20251024"      # stock KiCad 10 .kicad_sym version
GRID = 1.27

NS = uuid.uuid5(uuid.NAMESPACE_URL, "ee-portfolio/01-smart-power-node/hw/kicad")


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
    if lib_id == "Device:R_Shunt":      # R1 only: hide pin numbers (they overlap when rotated)
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


# ----------------------------------------------------------------- custom U1 symbol
# ESP32-DevKitC V4 headers, top to bottom, exactly as in Espressif's user guide
# (docs.espressif.com/projects/esp-dev-kits/en/latest/esp32/esp32-devkitc/user_guide.html,
#  "Header Block", tables J2 and J3). Type: P = power, I = input, I/O, G = ground.
J2_PINS = [("3V3", "P"), ("EN", "I"), ("VP", "I"), ("VN", "I"), ("IO34", "I"), ("IO35", "I"),
           ("IO32", "I/O"), ("IO33", "I/O"), ("IO25", "I/O"), ("IO26", "I/O"), ("IO27", "I/O"),
           ("IO14", "I/O"), ("IO12", "I/O"), ("GND", "G"), ("IO13", "I/O"), ("D2", "I/O"),
           ("D3", "I/O"), ("CMD", "I/O"), ("5V", "P")]
J3_PINS = [("GND", "G"), ("IO23", "I/O"), ("IO22", "I/O"), ("TX", "I/O"), ("RX", "I/O"),
           ("IO21", "I/O"), ("GND", "G"), ("IO19", "I/O"), ("IO18", "I/O"), ("IO5", "I/O"),
           ("IO17", "I/O"), ("IO16", "I/O"), ("IO4", "I/O"), ("IO0", "I/O"), ("IO2", "I/O"),
           ("IO15", "I/O"), ("D1", "I/O"), ("D0", "I/O"), ("CLK", "I/O")]
U1_NAME = "ESP32-DevKitC-V4"
U1_HALF_W, U1_PIN_LEN = 12.7, 3.81   # long pins so two-digit numbers clear the NC flags
U1_LIB_ID = PROJECT + ":" + U1_NAME
DEVKITC_URL = ("https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32/"
               "esp32-devkitc/user_guide.html")


def u1_pin_type(name, kind):
    if name == "3V3":
        return "power_out"
    if name == "5V":
        return "passive"          # unused here; the board is fed from its USB port
    if kind == "G":
        return "power_in"
    if kind == "I":
        return "input"            # EN and the input-only GPIO34/35/36(VP)/39(VN)
    return "bidirectional"


def font(size=1.27, bold=False):
    f = ["font", ["size", size, size]]
    if bold:
        f.append(["bold", "yes"])
    return f


def lib_prop(name, value, x, y, ang=0, hide=False, justify=None):
    p = ["property", Q(name), Q(value), ["at", x, y, ang], ["show_name", "no"],
         ["do_not_autoplace", "no"]]
    if hide:
        p.append(["hide", "yes"])
    eff = ["effects", font()]
    if justify:
        eff.append(["justify"] + justify.split())
    p.append(eff)
    return p


def u1_symbol(sym_name):
    """Build the ESP32-DevKitC-V4 symbol: J2 on the left, J3 on the right (board top view).
    Pin numbers 1-19 = J2.1-J2.19, 20-38 = J3.1-J3.19."""
    half_w, top, bot, pin_len = U1_HALF_W, 27.94, -25.4, U1_PIN_LEN
    gfx = [["rectangle", ["start", -half_w, top], ["end", half_w, bot],
            ["stroke", ["width", 0.254], ["type", "default"]], ["fill", ["type", "background"]]]]
    for txt, x, just in (("J2", -half_w + 1.27, "left"), ("J3", half_w - 1.27, "right")):
        gfx.append(["text", Q(txt), ["at", x, 25.4, 0], ["effects", font(), ["justify", just]]])
    gfx.append(["text", Q("USB"), ["at", 0, -22.86, 0], ["effects", font()]])
    pins = []
    for side, table, first in ((-1, J2_PINS, 1), (1, J3_PINS, 20)):
        for k, (name, kind) in enumerate(table):
            y = 22.86 - 2.54 * k
            pins.append(["pin", u1_pin_type(name, kind), "line",
                         ["at", side * (half_w + pin_len), y, 0 if side < 0 else 180],
                         ["length", pin_len],
                         ["name", Q(name), ["effects", font()]],
                         ["number", Q(str(first + k)), ["effects", font()]]])
    return ["symbol", Q(sym_name),
            ["pin_names", ["offset", 1.016]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            ["in_pos_files", "yes"], ["duplicate_pin_numbers_are_jumpers", "no"],
            lib_prop("Reference", "U", 0, 30.48),
            lib_prop("Value", U1_NAME, 0, 33.02),
            lib_prop("Footprint", "", 0, 0, hide=True),
            lib_prop("Datasheet", DEVKITC_URL, 0, 0, hide=True),
            lib_prop("Description",
                     "ESP32-DevKitC V4 (ESP32-WROOM-32E) dev board, both 19-pin headers per "
                     "Espressif user guide; pins 1-19 = J2.1-J2.19, pins 20-38 = J3.1-J3.19",
                     0, 0, hide=True),
            lib_prop("ki_keywords", "ESP32 DevKitC WROOM module board", 0, 0, hide=True),
            ["symbol", Q(U1_NAME + "_0_1")] + gfx,
            ["symbol", Q(U1_NAME + "_1_1")] + pins,
            ["embedded_fonts", "no"]]


def u1_pins():
    pins = {}
    for side, table, first in ((-1, J2_PINS, 1), (1, J3_PINS, 20)):
        for k, (name, kind) in enumerate(table):
            pins[str(first + k)] = dict(type=u1_pin_type(name, kind), x=side * (U1_HALF_W + U1_PIN_LEN),
                                        y=22.86 - 2.54 * k, name=name)
    return pins


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
        if lib_id == U1_LIB_ID:
            self.lib[lib_id] = u1_symbol(U1_LIB_ID)
            self.lib_pins[lib_id] = u1_pins()
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
            ds = DEVKITC_URL
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
               ["generator_version", Q("10.0")], ["uuid", Q(ROOT_UUID)], ["paper", Q("A4")],
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
FP_R0603 = "Resistor_SMD:R_0603_1608Metric"
FP_C0603 = "Capacitor_SMD:C_0603_1608Metric"
FP_SHUNT = "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm"   # 4-pad Kelvin 2512
FP_HDR2 = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
FP_HDR4 = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
FP_SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

NOTES = "\n".join([
    "DESIGN NOTES",
    "R1 shunt: 0.05 Ω, 1 %, 1 W, 2512, 4-terminal Kelvin (force pins 1-4, sense pins 2-3)",
    "  V_shunt = I x R = 2 A x 0.05 Ω = 100 mV at full load (PGA /4 range ±160 mV -> 3.2 A full scale)",
    "  P_shunt = I² x R = (2 A)² x 0.05 Ω = 0.2 W (5x margin on the 1 W part)",
    "  Load voltage at 2 A: 5.00 V - 0.10 V = 4.90 V",
    "Input filter: R2 = R3 = 10 Ω, C1 = 100 nF differential -> fc = 1 / (2π x 20 Ω x 100 nF) ≈ 80 kHz",
    "INA219 (U2): CONFIG = 0x1777 (BRNG 16 V, PG /4, BADC = SADC = 64-sample average, shunt + bus continuous)",
    "  CAL = trunc(0.04096 / (100 µA x 0.05 Ω)) = 8192; Current_LSB = 100 µA, Power_LSB = 2 mW",
    "  Bus voltage is read at IN- (load side) against GND: load ground and logic ground are common",
    "I2C: ESP32 GPIO21 = SDA, GPIO22 = SCL, 400 kHz, R4/R5 = 2.2 kΩ pull-ups to +3V3; U2 @ 0x40, OLED @ 0x3C",
    "Power: U1 (ESP32-DevKitC V4) runs from its USB port; its 3V3 pin feeds U2 and the OLED; its 5V pin is unused",
])


def build():
    s = Sheet()

    # ---------------- power path: J1 -> R1 (Kelvin shunt) -> J2 ------------------------
    ypp = 81.28
    j1 = s.place("J1", "Connector_Generic:Conn_01x02", "5V IN", 25.4, ypp, mirror="y",
                 footprint=FP_HDR2, fields={"Reference": (25.4, 77.47, None),
                                            "Value": (25.4, 88.9, None)})
    r1 = s.place("R1", "Device:R_Shunt", "0.05 1% 1W", 55.88, ypp, rot=90, footprint=FP_SHUNT,
                 fields={"Reference": (55.88, 85.09, None), "Value": (55.88, 87.63, None)})
    s.text("F 1/4  S 2/3", 52.07, 90.17, size=1.0, justify="left")
    j2 = s.place("J2", "Connector_Generic:Conn_01x02", "LOAD", 91.44, ypp, footprint=FP_HDR2,
                 fields={"Reference": (91.44, 77.47, None), "Value": (91.44, 88.9, None)})
    s.wire(j1["1"], r1["1"])
    s.label("VSUPPLY", 35.56, ypp)
    s.wire(r1["4"], j2["1"])
    s.label("VLOAD", 66.04, ypp)
    s.wire(j1["2"], (33.02, j1["2"][1]))
    s.gnd(33.02, j1["2"][1])
    s.wire(j2["2"], (83.82, j2["2"][1]))
    s.gnd(83.82, j2["2"][1])
    s.pwr_flag(43.18, 96.52)              # GND has only power-input pins otherwise
    s.gnd(43.18, 96.52)

    # ---------------- Kelvin sense + differential filter ------------------------------
    yp, yn = 55.88, 63.5
    r2 = s.place("R2", "Device:R", "10", 76.2, yp, rot=90, footprint=FP_R0603,
                 fields={"Reference": (76.2, 51.435, None), "Value": (76.2, 53.34, None)})
    r3 = s.place("R3", "Device:R", "10", 76.2, yn, rot=90, footprint=FP_R0603,
                 fields={"Reference": (76.2, 66.04, None), "Value": (76.2, 67.945, None)})
    c1 = s.place("C1", "Device:C", "100n", 87.63, 59.69, footprint=FP_C0603,
                 fields={"Reference": (84.836, 58.42, "right"), "Value": (84.836, 60.96, "right")})
    s.wire(r1["2"], (r1["2"][0], yp), r2["1"])
    s.label("SENSE_P", 55.88, yp)
    s.wire(r1["3"], (r1["3"][0], yn), r3["1"])
    s.label("SENSE_N", 60.96, yn)
    s.wire(r2["2"], c1["1"])
    s.wire(r3["2"], c1["2"])

    # ---------------- U2 INA219 -------------------------------------------------------
    u2 = s.place("U2", "Sensor_Energy:INA219AxD", "INA219AxD", 116.84, 58.42, footprint=FP_SOIC8,
                 fields={"Reference": (113.03, 67.31, "right"),
                         "Value": (113.03, 69.85, "right")})
    s.wire(c1["1"], u2["8"])                              # IN+ (pin 8) on the y = 55.88 row
    s.label("INA_INP", 90.17, yp)
    s.wire(c1["2"], (101.6, yn), (101.6, u2["7"][1]), u2["7"])   # IN- (pin 7)
    s.label("INA_INN", 90.17, yn)
    vs, yrail = u2["5"], 35.56
    s.wire(vs, (vs[0], yrail))
    s.v33(vs[0], yrail)
    c2 = s.place("C2", "Device:C", "100n", 109.22, 39.37, footprint=FP_C0603,
                 fields={"Reference": (106.426, 38.1, "right"), "Value": (106.426, 40.64, "right")})
    s.wire((vs[0], yrail), c2["1"])
    s.gnd(*c2["2"])
    s.gnd(*u2["6"])
    a1, a0 = u2["1"], u2["2"]
    xa = 129.54
    s.wire(a1, (xa, a1[1]))
    s.wire(a0, (xa, a0[1]))
    s.wire((xa, a1[1]), (xa, a0[1]))
    s.wire((xa, a0[1]), (xa, 66.04))
    s.gnd(xa, 66.04)

    # ---------------- I2C pull-ups ----------------------------------------------------
    r4 = s.place("R4", "Device:R", "2.2k", 137.16, 49.53, footprint=FP_R0603,
                 fields={"Reference": (135.255, 48.26, "right"), "Value": (135.255, 50.8, "right")})
    r5 = s.place("R5", "Device:R", "2.2k", 144.78, 52.07, footprint=FP_R0603,
                 fields={"Reference": (146.685, 50.8, "left"), "Value": (146.685, 53.34, "left")})
    s.wire(u2["3"], r4["2"])                              # SDA (pin 3)
    s.label("SDA", 128.27, u2["3"][1])
    s.wire(u2["4"], r5["2"])                              # SCL (pin 4)
    s.label("SCL", 128.27, u2["4"][1])
    ytop = 43.18
    s.wire(r4["1"], (r4["1"][0], ytop))
    s.wire(r5["1"], (r5["1"][0], ytop))
    s.wire((r4["1"][0], ytop), (r5["1"][0], ytop))
    s.v33(r4["1"][0], ytop)

    # ---------------- U1 ESP32-DevKitC V4 ---------------------------------------------
    u1 = s.place("U1", U1_LIB_ID, U1_NAME, 236.22, 68.58,
                 footprint="", fields={"Reference": (236.22, 35.56, None),
                                       "Value": (236.22, 38.1, None)})
    used = {"1", "14", "20", "22", "25", "26"}   # 3V3, GND(J2.14), GND(J3.1), IO22, IO21, GND(J3.7)
    s.wire(u1["1"], (215.9, u1["1"][1]))
    s.v33(215.9, u1["1"][1])
    s.wire(u1["14"], (215.9, u1["14"][1]))
    s.gnd(215.9, u1["14"][1])
    for n in ("20", "26"):
        s.wire(u1[n], (264.16, u1[n][1]))
        s.gnd(264.16, u1[n][1])
    s.wire(u1["22"], (256.54, u1["22"][1]))
    s.label("SCL", 256.54, u1["22"][1])
    s.wire(u1["25"], (256.54, u1["25"][1]))
    s.label("SDA", 256.54, u1["25"][1])
    for n in sorted(u1, key=int):
        if n not in used:
            s.nc(*u1[n])

    # ---------------- J3 SSD1306 OLED header ------------------------------------------
    # Mirrored vertically so pin 1 (GND) is at the bottom and VCC sits above it.
    j3 = s.place("J3", "Connector_Generic:Conn_01x04", "SSD1306 OLED", 175.26, 109.22,
                 mirror="x", footprint=FP_HDR4,
                 fields={"Reference": (178.435, 106.68, "left"),
                         "Value": (178.435, 109.22, "left")})
    s.wire(j3["4"], (162.56, j3["4"][1]))
    s.label("SDA", 162.56, j3["4"][1], left=True)
    s.wire(j3["3"], (162.56, j3["3"][1]))
    s.label("SCL", 162.56, j3["3"][1], left=True)
    c3 = s.place("C3", "Device:C", "100n", 160.02, 113.03, footprint=FP_C0603,
                 fields={"Reference": (157.226, 112.395, "right"),
                         "Value": (157.226, 114.935, "right")})
    s.wire(j3["2"], c3["1"])
    s.wire(c3["1"], (154.94, j3["2"][1]))
    s.v33(154.94, j3["2"][1])
    s.gnd(*c3["2"])
    s.wire(j3["1"], (167.64, j3["1"][1]))
    s.gnd(167.64, j3["1"][1])
    s.text("pin 1 GND, 2 VCC, 3 SCL, 4 SDA", 178.435, 111.76, size=1.0, justify="left")

    # ---------------- section titles and notes ----------------------------------------
    s.text("5 V POWER PATH (0-2 A)", 20.32, 72.39, size=1.524, bold=True, justify="left bottom")
    s.text("KELVIN SENSE + INA219 INPUT FILTER", 53.34, 46.99, size=1.524, bold=True,
           justify="left bottom")
    s.text("CURRENT / POWER MONITOR (I2C 0x40)", 99.06, 27.94, size=1.524, bold=True,
           justify="left bottom")
    s.text("I2C PULL-UPS", 132.08, 36.83, size=1.524, bold=True, justify="left bottom")
    s.text("MCU: ESP32-DevKitC V4 (headers J2 / J3)", 213.36, 27.94, size=1.524, bold=True,
           justify="left bottom")
    s.text("SSD1306 OLED HEADER (I2C 0x3C)", 147.32, 97.79, size=1.524, bold=True,
           justify="left bottom")
    s.text(NOTES, 20.32, 106.68, size=1.27, justify="left top")

    s.check()
    title = ["title_block",
             ["title", Q("Smart Power-Monitoring IoT Node")],
             ["date", Q("2026-10-27")],
             ["rev", Q("A")],
             ["company", Q("Shon Zinger - EE portfolio")],
             ["comment", 1, Q("INA219 @0x40 PGA/4 CAL=8192")],
             ["comment", 2, Q("SSD1306 @0x3C, I2C 400 kHz, 2.2k pull-ups")]]
    return s.sexpr(title)


def project_json():
    return json.dumps({
        "board": {"design_settings": {}},
        "boards": [],
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": PROJECT + ".kicad_pro", "version": 1},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [[ROOT_UUID, "Root"]],
        "text_variables": {},
    }, indent=2) + "\n"


def library_text():
    lib = ["kicad_symbol_lib", ["version", SYM_VERSION], ["generator", Q("gen_schematic.py")],
           ["generator_version", Q("10.0")], u1_symbol(U1_NAME)]
    return ser(lib) + "\n"


STOCK_SYMBOL_LIBS = ("Connector_Generic", "Device", "Sensor_Energy", "power")
STOCK_FOOTPRINT_LIBS = ("Capacitor_SMD", "Connector_PinHeader_2.54mm", "Package_SO", "Resistor_SMD")


def lib_table(kind, entries):
    rows = ['\t(lib (name "%s")(type "KiCad")(uri "%s")(options "")(descr "%s"))' % e for e in entries]
    return "(%s_lib_table\n\t(version 7)\n%s\n)\n" % (kind, "\n".join(rows))


# Project-level tables so ERC can resolve every library without a global KiCad setup.
SYM_LIB_TABLE = lib_table("sym", [(n, "${KICAD10_SYMBOL_DIR}/%s.kicad_sym" % n, "KiCad stock")
                                  for n in STOCK_SYMBOL_LIBS] +
                          [(PROJECT, "${KIPRJMOD}/%s.kicad_sym" % PROJECT,
                            "Project symbols: ESP32-DevKitC-V4")])
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
    print("wrote", os.path.join(HERE, PROJECT + ".kicad_sch"))
    if "--export" in sys.argv[1:]:
        export()


if __name__ == "__main__":
    main()
