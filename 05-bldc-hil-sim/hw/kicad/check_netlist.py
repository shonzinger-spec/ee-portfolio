#!/usr/bin/env python3
"""Export the KiCad netlist (kicadxml) and check every named net against the spec.

Each net below must contain exactly the listed REF.PIN members: nothing missing, nothing
extra. Any other net with two or more pins is also a failure (an unplanned connection).
The list is written by hand from the design, not from gen_schematic.py, so it is an
independent check of the generator. Prints PASS/FAIL per net; exits 1 on any failure.

Pin numbers: IR2101 1 VCC, 2 HIN, 3 LIN, 4 COM, 5 LO, 6 VS, 7 HO, 8 VB.
Q_NMOS_GDS 1 G, 2 D, 3 S. Device:D 1 K, 2 A. C_Polarized 1 +. R_Shunt 1/4 force, 2/3 sense.
INA181 1 OUT, 2 GND, 3 IN+, 4 IN-, 5 REF, 6 V+.
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCH = os.path.join(HERE, "bldc.kicad_sch")
KICAD_CLI = os.environ.get("KICAD_CLI",
                           "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

EXPECTED = {
    # power input and bus
    "VBUS_IN":  {"J1.1", "F1.1"},
    "VBUS":     {"F1.2", "C1.1", "C2.1", "Q1.2", "Q3.2", "Q5.2", "R51.1"},
    "+12V":     {"J2.1", "C3.1", "U1.1", "U2.1", "U3.1", "D1.2", "D2.2", "D3.2",
                 "C12.1", "C22.1", "C32.1", "J4.1"},
    "+3V3":     {"J5.1", "U4.6", "R41.1", "C42.1", "R61.1", "R62.1", "R63.1"},
    "GND":      {"J1.2", "J2.2", "J5.2", "J4.2", "C1.2", "C2.2", "C3.2",
                 "U1.4", "U2.4", "U3.4", "C12.2", "C22.2", "C32.2",
                 "R71.2", "R72.2", "R73.2", "R74.2", "R75.2", "R76.2",
                 "RS1.4", "U4.2", "R42.2", "C41.2", "C42.2", "C43.2",
                 "R52.2", "C51.2", "C61.2", "C62.2", "C63.2"},
    # low-side return through the shunt
    "LS_SRC":   {"Q2.3", "Q4.3", "Q6.3", "R14.2", "R24.2", "R34.2", "RS1.1"},
    "ISNS_P":   {"RS1.2", "U4.3"},
    "ISNS_N":   {"RS1.3", "U4.4"},
    "IREF":     {"R41.2", "R42.1", "C41.1", "U4.5"},
    "IAMP":     {"U4.1", "R43.1"},
    "ISENSE":   {"R43.2", "C43.1", "J5.12"},
    "VBUS_SNS": {"R51.2", "R52.1", "C51.1", "J5.13"},
}
# Leg n (A, B, C): driver Un, high side Q(2n-1), low side Q(2n), bootstrap Dn / Cn1,
# VCC decoupling Cn2, gate resistors Rn1 (high) / Rn2 (low), pull-downs Rn3 / Rn4,
# driver-input pull-downs R7(2n-1) / R7(2n), MCU header pins 3..8, motor terminal J3.n
for n, leg in enumerate("ABC", start=1):
    hi, lo = 2 * n - 1, 2 * n
    EXPECTED["PWM_%sH" % leg] = {"U%d.2" % n, "R7%d.1" % hi, "J5.%d" % (2 * n + 1)}
    EXPECTED["PWM_%sL" % leg] = {"U%d.3" % n, "R7%d.1" % lo, "J5.%d" % (2 * n + 2)}
    EXPECTED["HO_%s" % leg] = {"U%d.7" % n, "R%d1.1" % n}
    EXPECTED["LO_%s" % leg] = {"U%d.5" % n, "R%d2.1" % n}
    EXPECTED["GH_%s" % leg] = {"R%d1.2" % n, "R%d3.1" % n, "Q%d.1" % hi}
    EXPECTED["GL_%s" % leg] = {"R%d2.2" % n, "R%d4.1" % n, "Q%d.1" % lo}
    EXPECTED["VB_%s" % leg] = {"U%d.8" % n, "D%d.1" % n, "C%d1.1" % n}
    EXPECTED["SW_%s" % leg] = {"U%d.6" % n, "C%d1.2" % n, "R%d3.2" % n, "Q%d.3" % hi, "Q%d.2" % lo,
                               "J3.%d" % n}
    # Hall channel: pull-up R6n, filter R6(n+3) / C6n, header pins J4.(n+2) and J5.(n+8)
    EXPECTED["HRAW_%s" % leg] = {"J4.%d" % (n + 2), "R6%d.2" % n, "R6%d.1" % (n + 3)}
    EXPECTED["HALL_%s" % leg] = {"R6%d.2" % (n + 3), "C6%d.1" % n, "J5.%d" % (n + 8)}


def export_netlist(path):
    r = subprocess.run([KICAD_CLI, "sch", "export", "netlist", "--format", "kicadxml",
                        "-o", path, SCH], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(path):
        sys.exit("netlist export failed:\n" + r.stdout + r.stderr)


def read_nets(path):
    nets = {}
    for net in ET.parse(path).getroot().iter("net"):
        name = net.get("name")
        name = name[1:] if name.startswith("/") else name      # root-sheet local labels
        members = {"%s.%s" % (n.get("ref"), n.get("pin")) for n in net.iter("node")
                   if not n.get("ref", "").startswith("#")}
        nets[name] = nets.get(name, set()) | members
    return nets


def main():
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = os.path.join(tmp, "bldc.xml")
        export_netlist(xml_path)
        nets = read_nets(xml_path)

    failures = 0
    for name, want in EXPECTED.items():
        got = nets.get(name)
        if got is None:
            print("FAIL %-9s net not found" % name)
            failures += 1
            continue
        missing, extra = sorted(want - got), sorted(got - want)
        if missing or extra:
            print("FAIL %-9s missing=%s extra=%s" % (name, missing, extra))
            failures += 1
        else:
            print("PASS %-9s %s" % (name, " ".join(sorted(got))))

    for name, got in sorted(nets.items()):
        if name not in EXPECTED and len(got) > 1:
            print("FAIL unexpected net %s: %s" % (name, " ".join(sorted(got))))
            failures += 1
    singles = sum(1 for n, g in nets.items() if n not in EXPECTED and len(g) <= 1)
    print("info: %d single-pin nets (expected 0)" % singles)

    print("RESULT: %s (%d nets checked)" % ("PASS" if failures == 0 else "FAIL (%d)" % failures,
                                          len(EXPECTED)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
