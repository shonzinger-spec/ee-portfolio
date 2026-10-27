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
SCH = os.path.join(HERE, "power_node.kicad_sch")
KICAD_CLI = os.environ.get("KICAD_CLI",
                           "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

# U1 pin numbers: 1-19 = J2.1-J2.19, 20-38 = J3.1-J3.19 (Espressif DevKitC V4 header order).
EXPECTED = {
    "VSUPPLY": {"J1.1", "R1.1"},                       # R1.1 = supply-side force
    "VLOAD":   {"R1.4", "J2.1"},                       # R1.4 = load-side force
    "SENSE_P": {"R1.2", "R2.1"},                       # R1.2 = supply-side sense
    "SENSE_N": {"R1.3", "R3.1"},                       # R1.3 = load-side sense
    "INA_INP": {"R2.2", "C1.1", "U2.8"},               # U2.8 = IN+
    "INA_INN": {"R3.2", "C1.2", "U2.7"},               # U2.7 = IN-
    "SDA":     {"U1.25", "U2.3", "R4.2", "J3.4"},      # U1.25 = J3.6 IO21
    "SCL":     {"U1.22", "U2.4", "R5.2", "J3.3"},      # U1.22 = J3.3 IO22
    "+3V3":    {"U1.1", "U2.5", "C2.1", "R4.1", "R5.1", "J3.2", "C3.1"},   # U1.1 = 3V3, U2.5 = VS
    "GND":     {"U2.1", "U2.2", "U2.6", "J1.2", "J2.2", "J3.1", "C2.2", "C3.2",
                "U1.14", "U1.20", "U1.26"},            # U2 A1, A0, GND; U1 J2.14, J3.1, J3.7
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
        xml_path = os.path.join(tmp, "power_node.xml")
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
    print("info: %d single-pin nets (expected 32: the U1 pins with no-connect flags)" % singles)

    print("RESULT: %s" % ("PASS" if failures == 0 else "FAIL (%d)" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
