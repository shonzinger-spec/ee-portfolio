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
SCH = os.path.join(HERE, "sync_buck.kicad_sch")
KICAD_CLI = os.environ.get("KICAD_CLI",
                           "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

EXPECTED = {
    "VIN":    {"J1.1", "C1.1", "C2.1", "C3.1", "Q1.2", "U1.1", "C4.1",
               "R12.1"},
    "GND":    {"J1.2", "C1.2", "C2.2", "C3.2", "Q2.3", "R4.2", "C4.2", "U1.7", "C6.2", "C7.2",
               "C8.2", "J2.2", "U2.2", "U2.5", "C9.2", "C11.2", "R8.2", "C10.2", "J3.2",
               "R10.2", "R11.2", "R13.2", "C12.2"},
    "SW":     {"Q1.3", "Q2.2", "R3.2", "R5.1", "L1.1", "U1.4", "C5.2"},
    "GH":     {"Q1.1", "R3.1", "R1.2"},
    "GL":     {"Q2.1", "R4.1", "R2.2"},
    "HO":     {"U1.3", "R1.1"},
    "LO":     {"U1.8", "R2.1"},
    "HB":     {"U1.2", "C5.1"},                        # not in the brief's list: C5 + U1.HB
    "SNB":    {"R5.2", "C6.1"},
    "VOUT":   {"L1.2", "C7.1", "C8.1", "R6.1"},        # R6.1 = VOUT force
    "SH_P":   {"R6.2", "U2.3"},                        # R6.2 = VOUT sense, U2.3 = IN+
    "SH_N":   {"R6.3", "U2.4"},                        # R6.3 = load-side sense, U2.4 = IN-
    "VLOAD":  {"R6.4", "J2.1", "R7.1"},                # R6.4 = load-side force
    "VSENSE": {"R7.2", "R8.1", "C10.1", "J3.5"},
    "VINSENSE": {"R12.2", "R13.1", "C12.1", "J3.7"},
    "IAMP":   {"U2.1", "R9.1"},
    "ISENSE": {"R9.2", "C11.1", "J3.6"},
    "PWM_H":  {"U1.5", "R10.1", "J3.3"},
    "PWM_L":  {"U1.6", "R11.1", "J3.4"},
    "+3V3":   {"J3.1", "U2.6", "C9.1"},
}


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
        xml_path = os.path.join(tmp, "sync_buck.xml")
        export_netlist(xml_path)
        nets = read_nets(xml_path)

    failures = 0
    for name, want in EXPECTED.items():
        got = nets.get(name)
        if got is None:
            print("FAIL %-8s net not found" % name)
            failures += 1
            continue
        missing, extra = sorted(want - got), sorted(got - want)
        if missing or extra:
            print("FAIL %-8s missing=%s extra=%s" % (name, missing, extra))
            failures += 1
        else:
            print("PASS %-8s %s" % (name, " ".join(sorted(got))))

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
