#!/usr/bin/env python3
"""Export the KiCad netlist (kicadxml) and check every named net against the spec.

Each net below must contain exactly the listed REF.PIN members: nothing missing, nothing
extra. Any other net with two or more pins is also a failure (an unplanned connection).
Prints PASS/FAIL per net; exits 1 on any failure.
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCH = os.path.join(HERE, "bms.kicad_sch")
KICAD_CLI = os.environ.get("KICAD_CLI",
                           "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

EXPECTED = {
    # stack taps (B0 = GND)
    "GND":    {"J1.1", "J3.2", "C1.2", "C2.2", "R3.2", "C3.2", "C4.2",
               "C15.2", "C25.2", "C35.2", "C45.2", "R12.2", "R22.2", "R32.2", "R42.2", "R13.1",
               "U1.11", "U2.2", "TH1.2", "TH2.2", "TH3.2", "TH4.2", "C51.2", "C52.2", "C53.2", "C54.2",
               "Q3.2", "R71.2", "U3.2", "U4.2", "U5.2", "U6.2", "U7.2", "U8.2", "RS1.1"},
    "B1":     {"J1.2", "R11.1", "R23.1", "U3.4", "R61.2", "Q4.2", "R72.2"},
    "B2":     {"J1.3", "R21.1", "R33.1", "U4.4", "R62.2", "Q5.2", "R73.2"},
    "B3":     {"J1.4", "R31.1", "R43.1", "U5.4", "R63.2", "Q6.2", "R74.2"},
    "B4":     {"J1.5", "R41.1", "U6.4", "R64.2", "F1.1", "R1.1"},
    # power path: shunt, common-drain FETs, gate drive
    "PACK+":  {"F1.2", "J2.1"},
    "PACKN":  {"J2.2", "Q2.3", "R8.2", "D2.2"},
    "N_SH":   {"RS1.4", "Q1.3", "R7.2", "D1.2"},
    "MID":    {"Q1.2", "Q2.2"},
    "GD":     {"Q1.1", "R7.1", "D1.1", "U7.3"},
    "GC":     {"Q2.1", "R8.1", "D2.1", "U8.3"},
    "DSG_C":  {"R5.2", "U7.4"},
    "CHG_C":  {"R6.2", "U8.4"},
    "DSG_LED": {"R91.2", "U7.1"},
    "CHG_LED": {"R92.2", "U8.1"},
    "DSG_EN": {"R91.1", "J3.17"},
    "CHG_EN": {"R92.1", "J3.16"},
    "VAMP":   {"R1.2", "C1.1", "U1.4", "R5.1", "R6.1"},
    # current sense
    "ISNS_P": {"RS1.2", "U2.3"},
    "ISNS_N": {"RS1.3", "U2.4"},
    "+3V3":   {"J3.1", "U2.6", "C2.1", "R2.1", "R51.1", "R52.1", "R53.1", "R54.1"},
    "IREF":   {"R2.2", "R3.1", "C3.1", "U2.5"},
    "IAMP":   {"U2.1", "R4.1"},
    "ISENSE": {"R4.2", "C4.1", "J3.7"},
}
# Difference amplifier n: U1 unit n (+, -, out) = (3,2,1) (5,6,7) (10,9,8) (12,13,14)
for n, (p, m, o) in {1: ("3", "2", "1"), 2: ("5", "6", "7"), 3: ("10", "9", "8"), 4: ("12", "13", "14")}.items():
    EXPECTED["INP%d" % n] = {"R%d1.2" % n, "R%d2.1" % n, "U1." + p}
    EXPECTED["INN%d" % n] = {"R%d3.2" % n, "R%d4.1" % n, "U1." + m}
    EXPECTED["AMP%d" % n] = {"R%d4.2" % n, "R%d5.1" % n, "U1." + o}
    EXPECTED["VCELL%d" % n] = {"R%d5.2" % n, "C%d5.1" % n, "J3.%d" % (2 + n)}
    EXPECTED["TS%d" % n] = {"R5%d.2" % n, "TH%d.1" % n, "C5%d.1" % n, "J3.%d" % (7 + n)}
    # balancing: opto U(n+2) C=4 E=3 A=1, FET Q(n+2) AO3400A G=1 S=2 D=3
    EXPECTED["GB%d" % n] = {"Q%d.1" % (n + 2), "R7%d.1" % n, "U%d.3" % (n + 2)}
    EXPECTED["BLD%d" % n] = {"Q%d.3" % (n + 2), "R6%d.1" % n}
    EXPECTED["BAL%d" % n] = {"R8%d.1" % n, "J3.%d" % (11 + n)}
    EXPECTED["BAL%d_LED" % n] = {"R8%d.2" % n, "U%d.1" % (n + 2)}


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
        xml_path = os.path.join(tmp, "bms.xml")
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

    print("RESULT: %s" % ("PASS" if failures == 0 else "FAIL (%d)" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
