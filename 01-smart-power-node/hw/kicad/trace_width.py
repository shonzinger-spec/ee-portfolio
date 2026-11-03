#!/usr/bin/env python3
"""Size the PCB's power traces with the IPC-2221 outer-layer formula and check the traces as drawn.

    I = k * dT^0.44 * A^0.725        k = 0.048 (outer layers), dT in degC, A in mil^2, I in A

Prints the minimum width for the 2 A design load and for the INA219's 3.2 A full scale at a 10 degC
rise. Then, for the three connections that carry the load current (J1 pin 1 -> R1 pad 1 on VSUPPLY,
R1 pad 4 -> J2 pin 1 on VLOAD, and the ground return J2 pin 2 -> J1 pin 2), it looks in the board
file for tracks of that net, at least that minimum wide, that join the two pads, and reports their
length, narrowest width, temperature rise, DC resistance and drop. Exits 1 if any of the three is
not joined that way, however the tracks were drawn.
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
TRACES = (("/VSUPPLY", "J1.1", "R1.1"), ("/VLOAD", "R1.4", "J2.1"), ("GND", "J2.2", "J1.2"))
PAD_REACH = 0.8                # mm: a track end this close to a pad centre lands on the pad


def min_width_mm(amps, dt):
    area_mil2 = (amps / (K_OUTER * dt ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (COPPER_MM / MIL) * MIL


def rise_c(amps, width_mm):
    area_mil2 = (width_mm / MIL) * (COPPER_MM / MIL)
    return (amps / (K_OUTER * area_mil2 ** 0.725)) ** (1 / 0.44)


def mohm_per_mm(width_mm):
    return RHO_CU / (width_mm * 1e-3 * COPPER_MM * 1e-3) * 1e-3 * 1e3


def read_board(path):
    """Pad centres {"J1.1": (x, y)} and tracks [(x0, y0, x1, y1, width, net)] from the board file.
    Pad positions are stored in footprint coordinates; rotate them by the footprint angle."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    pads = {}
    for fp in re.finditer(r'\n\t\(footprint "[^"]+"(.*?)\n\t\)(?=\n)', text, re.S):
        body = fp.group(1)
        ref = re.search(r'\(property "Reference" "([^"]+)"', body).group(1)
        at = [float(v) for v in re.search(r'^\t\t\(at ([^)]*)\)', body, re.M).group(1).split()]
        fx, fy, ang = at[0], at[1], math.radians(at[2] if len(at) > 2 else 0.0)
        for number, pos in re.findall(r'\(pad "([^"]*)" \w+ \w+\s+\(at ([^)]*)\)', body):
            px, py = (float(v) for v in pos.split()[:2])
            pads["%s.%s" % (ref, number)] = (fx + px * math.cos(ang) + py * math.sin(ang),
                                              fy - px * math.sin(ang) + py * math.cos(ang))
    tracks = []
    for m in re.finditer(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)\s+"
                         r"\(width ([\d.]+)\).*?\(net \"([^\"]+)\"\)", text, re.S):
        tracks.append(tuple(float(v) for v in m.groups()[:5]) + (m.group(6),))
    return pads, tracks


def near(p, t, reach):
    """True if point p is within reach of track t's centre line."""
    (x, y), (x0, y0, x1, y1) = p, t[:4]
    dx, dy = x1 - x0, y1 - y0
    k = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - x0 - k * dx, y - y0 - k * dy) <= reach


def joining_tracks(pads, tracks, net, a, b, min_width):
    """Tracks on net, each at least min_width wide, connected to each other and touching both pads
    a and b; [] if no such chain exists."""
    wide = [t for t in tracks if t[5] == net and t[4] >= min_width]
    reached = [t for t in wide if near(pads[a], t, PAD_REACH)]
    frontier = list(reached)
    while frontier:
        t = frontier.pop()
        for u in wide:
            if u not in reached and any(near(e, u, u[4] / 2) for e in ((t[0], t[1]), (t[2], t[3]))):
                reached.append(u)
                frontier.append(u)
    return reached if any(near(pads[b], t, PAD_REACH) for t in reached) else []


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else PCB
    print("IPC-2221 outer layer, k = %.3f, 1 oz copper (%.0f um), allowed rise %.0f degC"
          % (K_OUTER, COPPER_MM * 1000, DT_MAX))
    for name, amps in CURRENTS:
        print("  %-18s %.1f A -> minimum width %.2f mm" % (name, amps, min_width_mm(amps, DT_MAX)))
    need = min_width_mm(CURRENTS[-1][1], DT_MAX)
    pads, tracks = read_board(path)
    print("Tracks of at least %.2f mm joining the pads, in %s:" % (need, os.path.basename(path)))
    print("%-9s %-11s %8s %8s %11s %11s %11s %13s" % ("net", "pads", "length", "width", "rise 2.0 A",
                                                      "rise 3.2 A", "R", "drop @ 2.0 A"))
    ok = True
    for net, a, b in TRACES:
        chain = joining_tracks(pads, tracks, net, a, b, need)
        if not chain:
            print("%-9s %-11s none: no track of at least %.2f mm joins them" % (net, a + "-" + b, need))
            ok = False
            continue
        length = sum(math.hypot(t[2] - t[0], t[3] - t[1]) for t in chain)
        width = min(t[4] for t in chain)
        r = sum(mohm_per_mm(t[4]) * math.hypot(t[2] - t[0], t[3] - t[1]) for t in chain)
        print("%-9s %-11s %5.1f mm %5.2f mm %7.1f degC %6.1f degC %6.2f mOhm %10.1f mV"
              % (net, a + "-" + b, length, width, rise_c(2.0, width), rise_c(3.2, width), r, r * 2.0))
    print("GND is the return trace alone; both GND pours run in parallel with it.")
    print("Shunt for comparison: 50 mOhm -> 100 mV at 2 A. The Kelvin taps sit on R1's sense pads, so these "
          "drops are outside the shunt measurement.")
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
