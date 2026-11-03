#!/usr/bin/env python3
"""Size the PCB's power traces with the IPC-2221 outer-layer formula and check the traces as drawn.

    I = k * dT^0.44 * A^0.725        k = 0.048 (outer layers), dT in degC, A in mil^2, I in A

Prints the minimum width for the 2 A design load and for the INA219's 3.2 A full scale at a 10 degC
rise, then reads the board file and, for the three traces that carry the load current (VSUPPLY,
VLOAD and the ground return, which gen_pcb.py puts in group GND_RETURN), reports the length and
narrowest width as drawn, the temperature rise at that width, and the DC resistance and drop
summed segment by segment. Exits 1 if one of the three is missing or narrower than the 3.2 A minimum.
Stdlib only:  python3 hw/kicad/trace_width.py [board.kicad_pcb]
"""
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PCB = os.path.join(HERE, "power_node.kicad_pcb")

K_OUTER = 0.048
COPPER_MM = 0.035              # 1 oz/ft^2, both outer layers
RHO_CU = 1.72e-8               # ohm m, copper at 20 degC
MIL = 0.0254                   # mm
DT_MAX = 10.0                  # degC rise allowed
CURRENTS = (("design load", 2.0), ("INA219 full scale", 3.2))   # 160 mV / 50 mOhm = 3.2 A
RETURN_GROUP = "GND_RETURN"


def min_width_mm(amps, dt):
    area_mil2 = (amps / (K_OUTER * dt ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (COPPER_MM / MIL) * MIL


def rise_c(amps, width_mm):
    area_mil2 = (width_mm / MIL) * (COPPER_MM / MIL)
    return (amps / (K_OUTER * area_mil2 ** 0.725)) ** (1 / 0.44)


def mohm_per_mm(width_mm):
    return RHO_CU / (width_mm * 1e-3 * COPPER_MM * 1e-3) * 1e-3 * 1e3


def power_traces(path):
    """{label: [(length mm, width mm), ...]} for VSUPPLY, VLOAD and the GND_RETURN group."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    group = re.search(r'\(group "%s".*?\(members(.*?)\)' % RETURN_GROUP, text, re.S)
    members = set(re.findall(r'"([0-9a-f-]{36})"', group.group(1))) if group else set()
    out = {"/VSUPPLY": [], "/VLOAD": [], "GND return": []}
    for m in re.finditer(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)\s+"
                         r"\(width ([\d.]+)\).*?\(net \"([^\"]+)\"\)\s+\(uuid \"([^\"]+)\"\)", text, re.S):
        x0, y0, x1, y1, width = (float(v) for v in m.groups()[:5])
        net, item = m.group(6), m.group(7)
        label = "GND return" if net == "GND" and item in members else net
        if label in out:
            out[label].append((math.hypot(x1 - x0, y1 - y0), width))
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else PCB
    print("IPC-2221 outer layer, k = %.3f, 1 oz copper (%.0f um), allowed rise %.0f degC"
          % (K_OUTER, COPPER_MM * 1000, DT_MAX))
    for name, amps in CURRENTS:
        print("  %-18s %.1f A -> minimum width %.2f mm" % (name, amps, min_width_mm(amps, DT_MAX)))
    need = min_width_mm(CURRENTS[-1][1], DT_MAX)
    print("Traces as drawn in %s:" % os.path.basename(path))
    print("%-11s %8s %8s %11s %11s %11s %13s" % ("trace", "length", "width", "rise 2.0 A", "rise 3.2 A",
                                                  "R", "drop @ 2.0 A"))
    ok = True
    for label, segs in power_traces(path).items():
        if not segs:
            print("%-11s missing" % label)
            ok = False
            continue
        length = sum(s[0] for s in segs)
        width = min(s[1] for s in segs)
        r = sum(mohm_per_mm(w) * l for l, w in segs)
        print("%-11s %5.1f mm %5.2f mm %7.1f degC %6.1f degC %6.2f mOhm %10.1f mV"
              % (label, length, width, rise_c(2.0, width), rise_c(3.2, width), r, r * 2.0))
        if width < need:
            print("  narrower than the %.2f mm IPC-2221 minimum at 3.2 A" % need)
            ok = False
    print("GND return is the trace alone; both GND pours run in parallel with it.")
    print("Shunt for comparison: 50 mOhm -> 100 mV at 2 A. The Kelvin taps sit on R1's sense pads, so these "
          "drops are outside the shunt measurement.")
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
