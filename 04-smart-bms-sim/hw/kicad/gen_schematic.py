#!/usr/bin/env python3
"""Regenerate the KiCad 10 schematic of the 04 smart BMS analog front end and power path.

Writes, next to this script:
  bms.kicad_sch   schematic (stock symbols copied verbatim from the KiCad libraries)
  bms.kicad_pro   minimal project file (ERC severities as in projects 01 and 02)
  sym-lib-table   project symbol library table
  fp-lib-table    project footprint library table

Stdlib only and deterministic: every UUID is uuid5() of a fixed name, no timestamps.

  python3 gen_schematic.py            regenerate the files above
  python3 gen_schematic.py --export   also write ../../docs/img/schematic.{svg,pdf,png}

Connectivity is by net label: every pin gets a 2.54 mm wire stub and a local label with its
net name, so the circuit is the PARTS table below and nothing else. check_netlist.py checks
the exported netlist against an independently written net list.
Coordinates are sheet millimetres (y grows downwards) on the 1.27 mm grid; check() asserts it.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "bms"
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
SYMDIR = os.environ.get("KICAD_SYMBOL_DIR", KICAD_APP + "/SharedSupport/symbols")
KICAD_CLI = os.environ.get("KICAD_CLI", KICAD_APP + "/MacOS/kicad-cli")
SCH_VERSION = "20260306"      # what KiCad 10.0.7 writes (same as project 02)
GRID = 1.27
STUB = 2.54

NS = uuid.uuid5(uuid.NAMESPACE_URL, "ee-portfolio/04-smart-bms-sim/hw/kicad")


def uid(*parts):
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


ROOT_UUID = uid("root-sheet")


# ----------------------------------------------------------------- copied from 02 unchanged
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
    return Raw(raw), parse_pins(block)


def font(size=1.27, bold=False):
    f = ["font", ["size", size, size]]
    if bold:
        f.append(["bold", "yes"])
    return f


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


# ----------------------------------------------------------------- multi-unit pins
def parse_pins(block):
    """number -> dict(unit, x, y, name, ang); unit 0 = pins common to every unit. ang is the
    pin's direction from its connection point into the body (library degrees)."""
    pins = {}
    subs = [(int(m.group(1)), m.start()) for m in re.finditer(r'\(symbol "[^"]+_(\d+)_\d+"', block)]
    for k, (unit, start) in enumerate(subs):
        end = subs[k + 1][1] if k + 1 < len(subs) else len(block)
        body = block[start:end]
        for m in re.finditer(r"\(pin (\w+) (\w+)\s*\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", body):
            pb = body[m.start():block_end(body, m.start()) + 1]
            number = re.search(r'\(number "((?:[^"\\]|\\.)*)"', pb).group(1)
            name = re.search(r'\(name "((?:[^"\\]|\\.)*)"', pb).group(1)
            pins[number] = dict(unit=unit, x=float(m.group(3)), y=float(m.group(4)), name=name,
                                ang=float(m.group(5)))
    return pins


def on_grid(v):
    return abs(v / GRID - round(v / GRID)) < 1e-6


def snap(v):
    return round(v, 4)


def g(n):
    return snap(n * GRID)


# ----------------------------------------------------------------- sheet
class Sheet:
    def __init__(self):
        self.lib, self.lib_pins = {}, {}
        self.symbols, self.wires, self.labels, self.texts = [], [], [], []
        self.points = []
        self.flg_n = 0

    def need(self, lib_id):
        if lib_id not in self.lib:
            self.lib[lib_id], self.lib_pins[lib_id] = stock_symbol(lib_id)

    @staticmethod
    def xform(px, py, rot):
        x, y = px, -py                     # library y-up -> sheet y-down
        return {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[rot]

    def place(self, ref, lib_id, value, x, y, rot=0, unit=1, footprint="", power=False, fields="tb"):
        """Place one unit; returns {pin number: (x, y, outward dx, outward dy)} for its pins.
        fields: "tb" = reference above and value below the pins' extent, "right" / "right_up" =
        both to the right of the body (symbols with labels above and below), "below" = both
        under the lowest pin."""
        self.need(lib_id)
        text = self.lib[lib_id].text
        m = re.search(r'\(property "Description" "((?:[^"\\]|\\.)*)"', text)
        desc = m.group(1).replace('\\"', '"') if m else ""
        m = re.search(r'\(property "Datasheet" "((?:[^"\\]|\\.)*)"', text)
        ds = m.group(1) if m else ""
        fang = 90 if rot in (90, 270) else 0      # field angles are relative to the symbol
        pins = {n: p for n, p in self.lib_pins[lib_id].items() if p["unit"] in (0, unit)}
        ys = [y + self.xform(p["x"], p["y"], rot)[1] for p in pins.values()]
        if fields == "right":
            rx, ry, vy, just = x + 5.08, y - 1.27, y + 1.27, "left"
        elif fields == "right_up":
            rx, ry, vy, just = x + 5.08, y - 5.08, y - 3.81, "left"
        elif fields == "below":
            rx, ry, vy, just = x, max(ys) + 3.81, max(ys) + 6.35, None
        else:
            two_pin = len(pins) == 2 and max(ys) == min(ys)
            m = 3.81 if two_pin else 5.08
            rx, ry, vy, just = x, min(ys) - m, max(ys) + m, None

        def prop(name, val, px, py, hide=False, just=None):
            return ["property", Q(name), Q(val), ["at", px, py, fang]] + (
                [["hide", "yes"]] if hide else []) + [
                ["show_name", "no"], ["do_not_autoplace", "no"],
                ["effects", font()] + ([["justify"] + just.split()] if just else [])]

        sym = ["symbol", ["lib_id", Q(lib_id)], ["at", x, y, rot], ["unit", unit], ["body_style", 1],
               ["exclude_from_sim", "no"], ["in_bom", "no" if power else "yes"],
               ["on_board", "no" if power else "yes"], ["in_pos_files", "no" if power else "yes"],
               ["dnp", "no"], ["uuid", Q(uid("sym", ref, unit))],
               prop("Reference", ref, rx, ry, hide=power, just=just),
               prop("Value", value, rx, vy, hide=power, just=just),
               prop("Footprint", footprint, x, y, hide=True),
               prop("Datasheet", ds, x, y, hide=True),
               prop("Description", desc, x, y, hide=True)]
        for number in sorted(pins, key=lambda n: (len(n), n)):
            sym.append(["pin", Q(number), ["uuid", Q(uid("pin", ref, number))]])
        sym.append(["instances", ["project", Q(PROJECT),
                                  ["path", Q("/" + ROOT_UUID), ["reference", Q(ref)], ["unit", unit]]]])
        self.symbols.append(sym)
        out = {}
        for number, p in pins.items():
            dx, dy = self.xform(p["x"], p["y"], rot)
            a = math.radians(p["ang"])
            ox, oy = self.xform(-round(math.cos(a)), -round(math.sin(a)), rot)   # outward
            out[number] = (snap(x + dx), snap(y + dy), ox, oy)
            self.points.append((out[number][0], out[number][1], "pin %s.%s" % (ref, number)))
        return out

    def stub(self, pin, net):
        """Wire 2.54 mm outward from the pin (opposite its direction into the body) and label it."""
        x, y, dx, dy = pin
        if abs(dx) >= abs(dy):
            d = 1 if dx > 0 else -1
            end, ang = (snap(x + d * STUB), y), (0 if d > 0 else 180)
        else:
            d = 1 if dy > 0 else -1
            end, ang = (x, snap(y + d * STUB)), (270 if d > 0 else 90)
        self.wires.append(((x, y), end))
        self.labels.append((net, end[0], end[1], ang))

    def pwr_flag(self, net, x, y):
        self.flg_n += 1
        self.place("#FLG%02d" % self.flg_n, "power:PWR_FLAG", "PWR_FLAG", x, y, power=True)
        self.wires.append(((x, y), (x, snap(y + STUB))))
        self.labels.append((net, x, snap(y + STUB), 270))
        self.points.append((x, y, "flag"))

    def text(self, s, x, y, size=1.27, bold=False):
        self.texts.append((s, x, y, size, bold))

    def check(self):
        pts = [(p[0], p[1]) for p in self.points] + [q for w in self.wires for q in w] + \
              [(l[1], l[2]) for l in self.labels]
        bad = [p for p in pts if not (on_grid(p[0]) and on_grid(p[1]))]
        if bad:
            raise SystemExit("off-grid connection points: %r" % bad[:5])
        ends = {}
        for a, b in self.wires:
            for p in (a, b):
                ends[p] = ends.get(p, 0) + 1
        if any(n > 1 for n in ends.values()):
            raise SystemExit("two wires share an end point: %r" % [p for p, n in ends.items() if n > 1])

    def sexpr(self, title_block):
        top = ["kicad_sch", ["version", SCH_VERSION], ["generator", Q("gen_schematic.py")],
               ["generator_version", Q("10.0")], ["uuid", Q(ROOT_UUID)], ["paper", Q("A2")],
               title_block]
        top.append(["lib_symbols"] + [self.lib[k] for k in sorted(self.lib)])
        for (a, b) in self.wires:
            top.append(["wire", ["pts", ["xy", a[0], a[1]], ["xy", b[0], b[1]]],
                        ["stroke", ["width", 0], ["type", "default"]],
                        ["uuid", Q(uid("wire", a, b))]])
        for (n, x, y, ang) in self.labels:
            top.append(["label", Q(n), ["at", x, y, ang],
                        ["effects", font(), ["justify", "left" if ang in (0, 90) else "right", "bottom"]],
                        ["uuid", Q(uid("label", n, x, y))]])
        for i, (s, x, y, size, bold) in enumerate(self.texts):
            top.append(["text", Q(s), ["exclude_from_sim", "no"], ["at", x, y, 0],
                        ["effects", font(size, bold), ["justify", "left", "top"]],
                        ["uuid", Q(uid("text", i))]])
        top += self.symbols
        top.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
        return ser(top) + "\n"


# ----------------------------------------------------------------- circuit
FP_R = "Resistor_SMD:R_0603_1608Metric"
FP_C = "Capacitor_SMD:C_0603_1608Metric"
FP_R1W = "Resistor_SMD:R_2512_6332Metric"
FP_OPTO = "Package_DIP:DIP-4_W7.62mm"
FP_HDR = "Connector_PinHeader_2.54mm:PinHeader_1x%02d_P2.54mm_Vertical"


def two(s, ref, lib, value, fp, x, y, n1, n2):
    """Two-pin part drawn horizontally: pin 1 (net n1) on the left, pin 2 (net n2) on the right."""
    rot = 0 if lib == "Device:D_Zener" else 90
    p = s.place(ref, lib, value, x, y, rot=rot, footprint=fp)
    s.stub(p["1"], n1)
    s.stub(p["2"], n2)


def multi(s, ref, lib, value, fp, x, y, nets, unit=1, rot=0, fields="tb"):
    p = s.place(ref, lib, value, x, y, rot=rot, unit=unit, footprint=fp, fields=fields)
    assert set(nets) == set(p), (ref, sorted(p), sorted(nets))
    for number, net in nets.items():
        s.stub(p[number], net)


def tap(n):
    return "GND" if n == 0 else "B%d" % n


NOTES = """Simulated design only: nothing built in hardware.
B0 = GND = negative end of the 4S stack. Charge current flows B0 -> RS1 -> N_SH.
Difference amplifiers: Vout = 0.5 x (B(n) - B(n-1)); 2M / 1M networks matched to 0.01 %
(sim/ngspice/cell_sense.cir: worst case 4.5 mV; 0.1 % parts give 37.7 mV).
VAMP (amplifier supply and FET gate drive) is always on in this sheet: 4 mA from the stack.
Q1 discharge / Q2 charge FETs are common-drain (MID); each body diode passes the other direction.
ISENSE = 1.65 V + 20 mV/A (charge positive). TSn: 10k pull-up, 10k B3950 NTC to GND.
J3 goes to an ESP32-S3: VCELL1-4, ISENSE, TS1-4 on ADC1 (GPIO1-9), BAL1-4 GPIO11-14,
CHG_EN GPIO15, DSG_EN GPIO16."""


def build():
    s = Sheet()
    hdr = lambda t, x, y: s.text(t, x, y, size=2.0, bold=True)

    # Cell taps, pack terminals and power path
    hdr("Cell taps, pack terminals, power path", g(10), g(10))
    multi(s, "J1", "Connector_Generic:Conn_01x05", "Cell taps", FP_HDR % 5, g(20), g(24),
          {"1": "GND", "2": "B1", "3": "B2", "4": "B3", "5": "B4"})
    multi(s, "J2", "Connector_Generic:Conn_01x02", "Pack", "Connector_AMASS:AMASS_XT60-M_1x02_P7.20mm_Vertical",
          g(20), g(44), {"1": "PACK+", "2": "PACKN"})
    two(s, "F1", "Device:Fuse", "15A", "Fuse:Fuse_Blade_ATO_directSolder", g(52), g(22), "B4", "PACK+")
    multi(s, "RS1", "Device:R_Shunt", "1m", "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm",
          g(52), g(40), {"1": "GND", "2": "ISNS_P", "3": "ISNS_N", "4": "N_SH"}, rot=90, fields="below")
    nmos = "Transistor_FET:Q_NMOS_GDS"
    multi(s, "Q1", nmos, "IRLB3034", "Package_TO_SOT_THT:TO-220-3_Vertical",
          g(32), g(66), {"1": "GD", "2": "MID", "3": "N_SH"}, fields="right")
    multi(s, "Q2", nmos, "IRLB3034", "Package_TO_SOT_THT:TO-220-3_Vertical",
          g(58), g(66), {"1": "GC", "2": "MID", "3": "PACKN"}, fields="right")
    for i, (ref, val, n1, n2) in enumerate([("D1", "12V", "GD", "N_SH"), ("D2", "12V", "GC", "PACKN")]):
        two(s, ref, "Device:D_Zener", val, "Diode_SMD:D_SOD-123", g(26 + 30 * i), g(88), n1, n2)
    for i, (ref, n1, n2) in enumerate([("R7", "GD", "N_SH"), ("R8", "GC", "PACKN")]):
        two(s, ref, "Device:R", "100k", FP_R, g(26 + 30 * i), g(96), n1, n2)

    # FET drivers and amplifier supply
    hdr("FET drivers (opto, gate clamp)", g(90), g(10))
    opto = "Isolator:PC817"
    multi(s, "U7", opto, "PC817", FP_OPTO, g(104), g(22), {"1": "DSG_LED", "2": "GND", "3": "GD", "4": "DSG_C"})
    multi(s, "U8", opto, "PC817", FP_OPTO, g(104), g(40), {"1": "CHG_LED", "2": "GND", "3": "GC", "4": "CHG_C"})
    for i, (ref, val, n1, n2) in enumerate([("R5", "10k", "VAMP", "DSG_C"), ("R6", "10k", "VAMP", "CHG_C"),
                                            ("R91", "1k", "DSG_EN", "DSG_LED"), ("R92", "1k", "CHG_EN", "CHG_LED")]):
        two(s, ref, "Device:R", val, FP_R, g(104), g(56 + 8 * i), n1, n2)
    hdr("Amplifier supply", g(90), g(92))
    two(s, "R1", "Device:R", "100", FP_R, g(104), g(100), "B4", "VAMP")
    two(s, "C1", "Device:C", "1u 25V", FP_C, g(104), g(108), "VAMP", "GND")
    hdr("Power flags (ERC)", g(90), g(116))
    for i, net in enumerate(("VAMP", "GND", "+3V3")):
        s.pwr_flag(net, g(96 + 10 * i), g(124))

    # Difference amplifiers
    hdr("Cell voltage: difference amplifiers, gain 0.5", g(140), g(10))
    amp_pins = {1: ("3", "2", "1"), 2: ("5", "6", "7"), 3: ("10", "9", "8"), 4: ("12", "13", "14")}
    for n in range(1, 5):
        y = g(24 + 26 * (n - 1))
        plus, minus, out = amp_pins[n]
        multi(s, "U1", "Amplifier_Operational:LM2902", "OPA4197", "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
              g(152), y, {plus: "INP%d" % n, minus: "INN%d" % n, out: "AMP%d" % n}, unit=n)
        rows = [("R%d1" % n, "R", "2M 0.01%", "B%d" % n, "INP%d" % n),
                ("R%d2" % n, "R", "1M 0.01%", "INP%d" % n, "GND"),
                ("R%d3" % n, "R", "2M 0.01%", tap(n - 1), "INN%d" % n),
                ("R%d4" % n, "R", "1M 0.01%", "INN%d" % n, "AMP%d" % n),
                ("R%d5" % n, "R", "10k", "AMP%d" % n, "VCELL%d" % n),
                ("C%d5" % n, "C", "1u", "VCELL%d" % n, "GND")]
        for k, (ref, kind, val, n1, n2) in enumerate(rows):
            two(s, ref, "Device:" + kind, val, FP_R if kind == "R" else FP_C,
                g(180 + 28 * (k // 3)), y + g(8 * (k % 3) - 8), n1, n2)
    multi(s, "U1", "Amplifier_Operational:LM2902", "OPA4197", "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
          g(152), g(130), {"4": "VAMP", "11": "GND"}, unit=5, fields="right")

    # Current sense
    hdr("Pack current: 1 mOhm shunt + INA181A1", g(240), g(10))
    multi(s, "U2", "Amplifier_Current:INA181", "INA181A1", "Package_TO_SOT_SMD:SOT-23-6",
          g(252), g(24), {"1": "IAMP", "2": "GND", "3": "ISNS_P", "4": "ISNS_N", "5": "IREF", "6": "+3V3"}, fields="right_up")
    rows = [("C2", "C", "100n", "+3V3", "GND"), ("R2", "R", "10k", "+3V3", "IREF"),
            ("R3", "R", "10k", "IREF", "GND"), ("C3", "C", "100n", "IREF", "GND"),
            ("R4", "R", "1k", "IAMP", "ISENSE"), ("C4", "C", "100n", "ISENSE", "GND")]
    for k, (ref, kind, val, n1, n2) in enumerate(rows):
        two(s, ref, "Device:" + kind, val, FP_R if kind == "R" else FP_C, g(246 + 28 * (k // 3)),
            g(40 + 8 * (k % 3)), n1, n2)

    # Temperature
    hdr("Cell temperature: NTC dividers", g(240), g(68))
    for n in range(1, 5):
        y = g(76 + 8 * (n - 1))
        two(s, "R5%d" % n, "Device:R", "10k", FP_R, g(246), y, "+3V3", "TS%d" % n)
        two(s, "TH%d" % n, "Device:Thermistor_NTC", "10k B3950", FP_R, g(274), y, "TS%d" % n, "GND")
        two(s, "C5%d" % n, "Device:C", "100n", FP_C, g(302), y, "TS%d" % n, "GND")

    # Balancing
    hdr("Passive balancing: 33 ohm bleed per cell", g(320), g(10))
    for n in range(1, 5):
        y = g(26 + 26 * (n - 1))
        multi(s, "U%d" % (n + 2), opto, "PC817", FP_OPTO, g(332), y,
              {"1": "BAL%d_LED" % n, "2": "GND", "3": "GB%d" % n, "4": "B%d" % n})
        multi(s, "Q%d" % (n + 2), "Transistor_FET:Q_NMOS_GSD", "AO3400A", "Package_TO_SOT_SMD:SOT-23",
              g(358), y, {"1": "GB%d" % n, "2": tap(n - 1), "3": "BLD%d" % n}, fields="right")
        two(s, "R6%d" % n, "Device:R", "33 1W", FP_R1W, g(388), y - g(8), "BLD%d" % n, "B%d" % n)
        two(s, "R7%d" % n, "Device:R", "100k", FP_R, g(388), y, "GB%d" % n, tap(n - 1))
        two(s, "R8%d" % n, "Device:R", "1k", FP_R, g(388), y + g(8), "BAL%d" % n, "BAL%d_LED" % n)

    # MCU header
    hdr("ESP32-S3 header", g(410), g(10))
    j3 = ["+3V3", "GND", "VCELL1", "VCELL2", "VCELL3", "VCELL4", "ISENSE", "TS1", "TS2", "TS3", "TS4",
          "BAL1", "BAL2", "BAL3", "BAL4", "CHG_EN", "DSG_EN"]
    multi(s, "J3", "Connector_Generic:Conn_01x17", "ESP32-S3", FP_HDR % 17, g(430), g(40),
          {str(i + 1): n for i, n in enumerate(j3)})

    s.text(NOTES, g(10), g(150), size=1.5)
    s.check()
    tb = ["title_block",
          ["title", Q("04 - Smart BMS, 4S Li-ion: analog front end and power path (simulated)")],
          ["date", Q("2026-11-03")],
          ["rev", Q("A")],
          ["company", Q("Shon Zinger")],
          ["comment", 1, Q("Simulated design only: nothing built in hardware")],
          ["comment", 2, Q("U1 OPA4197 difference amplifiers, U2 INA181A1, PC817 opto FET and bleed drive")]]
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


STOCK_SYMBOL_LIBS = ("Amplifier_Current", "Amplifier_Operational", "Connector_Generic", "Device",
                     "Isolator", "Transistor_FET", "power")
STOCK_FOOTPRINT_LIBS = ("Capacitor_SMD", "Connector_AMASS", "Connector_PinHeader_2.54mm", "Diode_SMD",
                        "Fuse", "Package_DIP", "Package_SO", "Package_TO_SOT_SMD", "Package_TO_SOT_THT",
                        "Resistor_SMD")


def lib_table(kind, entries):
    rows = ['\t(lib (name "%s")(type "KiCad")(uri "%s")(options "")(descr "%s"))' % e for e in entries]
    return "(%s_lib_table\n\t(version 7)\n%s\n)\n" % (kind, "\n".join(rows))


SYM_LIB_TABLE = lib_table("sym", [(n, "${KICAD10_SYMBOL_DIR}/%s.kicad_sym" % n, "KiCad stock")
                                  for n in STOCK_SYMBOL_LIBS])
FP_LIB_TABLE = lib_table("fp", [(n, "${KICAD10_FOOTPRINT_DIR}/%s.pretty" % n, "KiCad stock")
                                for n in STOCK_FOOTPRINT_LIBS])


def main():
    write(os.path.join(HERE, PROJECT + ".kicad_sch"), build())
    write(os.path.join(HERE, PROJECT + ".kicad_pro"), project_json())
    write(os.path.join(HERE, "sym-lib-table"), SYM_LIB_TABLE)
    write(os.path.join(HERE, "fp-lib-table"), FP_LIB_TABLE)
    print("wrote", PROJECT + ".kicad_sch")
    if "--export" in sys.argv[1:]:
        export()


if __name__ == "__main__":
    main()
