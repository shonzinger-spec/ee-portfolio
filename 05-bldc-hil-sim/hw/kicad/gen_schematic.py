#!/usr/bin/env python3
"""Regenerate the KiCad 10 schematic of the 05 BLDC 3-phase inverter, gate drive and sensing.

Writes, next to this script:
  bldc.kicad_sch  schematic (stock symbols copied verbatim from the KiCad libraries)
  bldc.kicad_pro  minimal project file (ERC severities as in projects 01 and 02)
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
PROJECT = "bldc"
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
SYMDIR = os.environ.get("KICAD_SYMBOL_DIR", KICAD_APP + "/SharedSupport/symbols")
KICAD_CLI = os.environ.get("KICAD_CLI", KICAD_APP + "/MacOS/kicad-cli")
SCH_VERSION = "20260306"      # what KiCad 10.0.7 writes (same as project 02)
GRID = 1.27
STUB = 2.54

NS = uuid.uuid5(uuid.NAMESPACE_URL, "ee-portfolio/05-bldc-hil-sim/hw/kicad")


def uid(*parts):
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


ROOT_UUID = uid("root-sheet")


# ----------------------------------------------------------------- copied from 04 unchanged
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
FP_C1206 = "Capacitor_SMD:C_1206_3216Metric"
FP_HDR = "Connector_PinHeader_2.54mm:PinHeader_1x%02d_P2.54mm_Vertical"
FP_FET = "Package_TO_SOT_SMD:TO-252-2"
FP_DRV = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"


def two(s, ref, lib, value, fp, x, y, n1, n2):
    """Two-pin part drawn horizontally: pin 1 (net n1) on the left, pin 2 (net n2) on the right.
    For diodes pin 1 is the cathode."""
    rot = 0 if lib in ("Device:D", "Device:D_Schottky") else 90
    p = s.place(ref, lib, value, x, y, rot=rot, footprint=fp)
    s.stub(p["1"], n1)
    s.stub(p["2"], n2)


def multi(s, ref, lib, value, fp, x, y, nets, unit=1, rot=0, fields="tb"):
    p = s.place(ref, lib, value, x, y, rot=rot, unit=unit, footprint=fp, fields=fields)
    assert set(nets) == set(p), (ref, sorted(p), sorted(nets))
    for number, net in nets.items():
        s.stub(p[number], net)


NOTES = """Simulated design only: nothing built in hardware.
Six-step (trapezoidal) drive: in each 60 deg sector one high side switches at 20 kHz with its low side as
synchronous rectifier, one low side is on, one leg floats. Dead time (500 ns) is inserted by the firmware
(compare pair per leg, src/bldc/bldc.c); the IR2101 has no internal interlock, so the MCU timing is the
only protection against shoot-through (sim/ngspice/leg_deadtime.cir: shoot-through below about 250 ns).
Gate pull-downs (R13/R14...) hold the bridge off while the MCU is in reset; R71-R76 hold the driver inputs low.
RS1 2.5 mOhm + U4 INA181A1 (gain 20): ISENSE = 1.65 V + 50 mV/A, sampled at the PWM counter peak.
VBUS_SNS = VBUS / 11. Hall sensors: open collector, 12 V supply, 4.7k pull-ups to 3.3 V, 1k / 1 nF filter.
J5 goes to the MCU: PWM_xH / PWM_xL from a centre-aligned timer, HALL_A-C to GPIO, ISENSE / VBUS_SNS to ADC."""

LEGS = ("A", "B", "C")


def build():
    s = Sheet()
    hdr = lambda t, x, y: s.text(t, x, y, size=2.0, bold=True)

    # Power input
    hdr("Power input, bus capacitors", g(10), g(10))
    multi(s, "J1", "Connector_Generic:Conn_01x02", "24 V in", "Connector_AMASS:AMASS_XT60-M_1x02_P7.20mm_Vertical",
          g(20), g(24), {"1": "VBUS_IN", "2": "GND"})
    two(s, "F1", "Device:Fuse", "15A", "Fuse:Fuse_Blade_ATO_directSolder", g(46), g(22), "VBUS_IN", "VBUS")
    two(s, "C1", "Device:C", "10u 50V", FP_C1206, g(46), g(30), "VBUS", "GND")
    two(s, "C2", "Device:C_Polarized", "470u 35V", "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm",
        g(46), g(38), "VBUS", "GND")
    multi(s, "J2", "Connector_Generic:Conn_01x02", "+12 V gate supply", FP_HDR % 2, g(20), g(52),
          {"1": "+12V", "2": "GND"})
    two(s, "C3", "Device:C", "10u 25V", FP_C1206, g(46), g(50), "+12V", "GND")
    multi(s, "J3", "Connector_Generic:Conn_01x03", "Motor",
          "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3_1x03_P5.00mm_Horizontal",
          g(20), g(72), {"1": "SW_A", "2": "SW_B", "3": "SW_C"})
    hdr("Power flags (ERC)", g(10), g(88))
    for i, net in enumerate(("VBUS", "GND", "+12V", "+3V3")):
        s.pwr_flag(net, g(16 + 10 * i), g(96))

    # Three half-bridges: IR2101 driver, bootstrap, gate resistors and pull-downs, MOSFETs
    for n, leg in enumerate(LEGS, start=1):
        x0 = g(70 + 80 * (n - 1))
        hdr("Leg %s: IR2101 + half-bridge" % leg, x0, g(10))
        multi(s, "U%d" % n, "Driver_FET:IR2101", "IR2101", FP_DRV, x0 + g(14), g(30),
              {"1": "+12V", "2": "PWM_%sH" % leg, "3": "PWM_%sL" % leg, "4": "GND",
               "5": "LO_%s" % leg, "6": "SW_%s" % leg, "7": "HO_%s" % leg, "8": "VB_%s" % leg})
        two(s, "D%d" % n, "Device:D", "ES1D", "Diode_SMD:D_SMA", x0 + g(10), g(50), "VB_%s" % leg, "+12V")
        two(s, "C%d1" % n, "Device:C", "1u 25V", FP_C, x0 + g(10), g(58), "VB_%s" % leg, "SW_%s" % leg)
        two(s, "C%d2" % n, "Device:C", "1u 25V", FP_C, x0 + g(10), g(66), "+12V", "GND")
        rows = [("R%d1" % n, "10", "HO_%s" % leg, "GH_%s" % leg),
                ("R%d3" % n, "10k", "GH_%s" % leg, "SW_%s" % leg),
                ("R%d2" % n, "10", "LO_%s" % leg, "GL_%s" % leg),
                ("R%d4" % n, "10k", "GL_%s" % leg, "LS_SRC")]
        for k, (ref, val, n1, n2) in enumerate(rows):
            two(s, ref, "Device:R", val, FP_R, x0 + g(40), g(50 + 8 * k), n1, n2)
        nmos = "Transistor_FET:Q_NMOS_GDS"
        multi(s, "Q%d" % (2 * n - 1), nmos, "IRFR4104", FP_FET, x0 + g(14), g(90),
              {"1": "GH_%s" % leg, "2": "VBUS", "3": "SW_%s" % leg}, fields="right")
        multi(s, "Q%d" % (2 * n), nmos, "IRFR4104", FP_FET, x0 + g(14), g(118),
              {"1": "GL_%s" % leg, "2": "SW_%s" % leg, "3": "LS_SRC"}, fields="right")
        for k, (pin_net, ref) in enumerate((("PWM_%sH" % leg, "R7%d" % (2 * n - 1)),
                                            ("PWM_%sL" % leg, "R7%d" % (2 * n)))):
            two(s, ref, "Device:R", "10k", FP_R, x0 + g(40), g(90 + 8 * k), pin_net, "GND")

    # Current sense
    y0 = g(130)
    hdr("DC-link current sense (low side)", g(70), y0)
    multi(s, "RS1", "Device:R_Shunt", "2.5m", "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm",
          g(80), y0 + g(18), {"1": "LS_SRC", "2": "ISNS_P", "3": "ISNS_N", "4": "GND"}, rot=90, fields="below")
    multi(s, "U4", "Amplifier_Current:INA181", "INA181A1", "Package_TO_SOT_SMD:SOT-23-6",
          g(106), y0 + g(18), {"1": "IAMP", "2": "GND", "3": "ISNS_P", "4": "ISNS_N", "5": "IREF", "6": "+3V3"})
    for k, (ref, kind, val, n1, n2) in enumerate([
            ("R41", "R", "10k", "+3V3", "IREF"), ("R42", "R", "10k", "IREF", "GND"),
            ("C41", "C", "100n", "IREF", "GND"), ("C42", "C", "100n", "+3V3", "GND"),
            ("R43", "R", "100", "IAMP", "ISENSE"), ("C43", "C", "1n", "ISENSE", "GND")]):
        two(s, ref, "Device:" + kind, val, FP_R if kind == "R" else FP_C,
            g(132 + 28 * (k // 3)), y0 + g(10 + 8 * (k % 3)), n1, n2)

    # Bus voltage sense
    hdr("Bus voltage sense", g(70), y0 + g(40))
    for k, (ref, kind, val, n1, n2) in enumerate([
            ("R51", "R", "100k", "VBUS", "VBUS_SNS"), ("R52", "R", "10k", "VBUS_SNS", "GND"),
            ("C51", "C", "100n", "VBUS_SNS", "GND")]):
        two(s, ref, "Device:" + kind, val, FP_R if kind == "R" else FP_C, g(80), y0 + g(50 + 8 * k), n1, n2)

    # Hall sensors
    hdr("Hall sensor inputs", g(200), y0)
    multi(s, "J4", "Connector_Generic:Conn_01x05", "Hall", FP_HDR % 5, g(210), y0 + g(16),
          {"1": "+12V", "2": "GND", "3": "HRAW_A", "4": "HRAW_B", "5": "HRAW_C"})
    for k, leg in enumerate(LEGS):
        y = y0 + g(10 + 10 * k)
        two(s, "R6%d" % (k + 1), "Device:R", "4.7k", FP_R, g(236), y, "+3V3", "HRAW_%s" % leg)
        two(s, "R6%d" % (k + 4), "Device:R", "1k", FP_R, g(262), y, "HRAW_%s" % leg, "HALL_%s" % leg)
        two(s, "C6%d" % (k + 1), "Device:C", "1n", FP_C, g(288), y, "HALL_%s" % leg, "GND")

    # MCU header
    hdr("MCU header", g(200), y0 + g(46))
    j5 = ["+3V3", "GND", "PWM_AH", "PWM_AL", "PWM_BH", "PWM_BL", "PWM_CH", "PWM_CL",
          "HALL_A", "HALL_B", "HALL_C", "ISENSE", "VBUS_SNS"]
    multi(s, "J5", "Connector_Generic:Conn_01x13", "MCU", FP_HDR % 13, g(210), y0 + g(70),
          {str(i + 1): n for i, n in enumerate(j5)})

    s.text(NOTES, g(10), g(230), size=1.5)
    s.check()
    tb = ["title_block",
          ["title", Q("05 - BLDC 6-step controller: inverter and gate drive (simulated)")],
          ["date", Q("2026-11-03")],
          ["rev", Q("A")],
          ["company", Q("Shon Zinger")],
          ["comment", 1, Q("Simulated design only: nothing built in hardware")],
          ["comment", 2, Q("3 x IR2101, 6 x IRFR4104, 2.5 mOhm shunt + INA181A1, Hall inputs, MCU header")]]
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


STOCK_SYMBOL_LIBS = ("Amplifier_Current", "Connector_Generic", "Device", "Driver_FET", "Transistor_FET",
                     "power")
STOCK_FOOTPRINT_LIBS = ("Capacitor_SMD", "Capacitor_THT", "Connector_AMASS", "Connector_PinHeader_2.54mm",
                        "Diode_SMD", "Fuse", "Package_SO", "Package_TO_SOT_SMD", "Resistor_SMD",
                        "TerminalBlock_Phoenix")


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
