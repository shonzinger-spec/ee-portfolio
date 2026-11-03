#!/usr/bin/env python3
"""Size the buck's power copper with the IPC-2221 outer-layer formula and check the board as drawn.

    I = k * dT^0.44 * A^0.725        k = 0.048 (outer layers), dT in degC, A in mil^2, I in A

Prints the minimum width at a 10 degC rise for the 3 A output current and for the 4.5 A
over-current trip level (BUCK_OCP in src/buck_ctrl/buck_config.h; a 3-sample latch, so it is
carried for 60 us, not continuously). Every power connection is sized for the 3 A output current,
the input side included: the hot loop between Q1's tab and C1/C2 carries the full inductor current
in pulses, and J1 -> Q1 carries less than that.
Then, for each power connection in CONNECTIONS, it looks in the board file for the shortest chain of
F.Cu tracks of that net, each at least the 3 A minimum wide, that joins the two pads (a track counts as touching a pad when its
copper overlaps the pad rectangle), and reports length, narrowest width, temperature rise at 3 A
and 4.5 A, DC resistance and drop at 3 A. Exits 1 if any connection is not joined that way, however
the tracks were drawn.
Stdlib only:  python3 hw/kicad/trace_width.py [board.kicad_pcb]
"""
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PCB = os.path.join(HERE, "sync_buck.kicad_pcb")

K_OUTER = 0.048
COPPER_MM = 0.035              # 1 oz/ft^2, both outer layers
RHO_CU = 1.72e-8               # ohm m, copper at 20 degC
MIL = 0.0254                   # mm
DT_MAX = 10.0                  # degC rise allowed
I_DESIGN = 3.0                 # A, rated output current
I_OCP = 4.5                    # A, over-current latch threshold
# net, pad A, pad B   (Q1.2T / Q2.2T: the DPAK tab, the larger of the two pads numbered 2)
CONNECTIONS = (
    ("/VIN", "J1.1", "C3.1"), ("/VIN", "C3.1", "Q1.2T"), ("/VIN", "Q1.2T", "C1.1"), ("/VIN", "C1.1", "C2.1"),
    ("/SW", "Q1.3", "Q2.2T"), ("/SW", "Q1.3", "L1.1"),
    ("/VOUT", "L1.2", "C7.1"), ("/VOUT", "L1.2", "C8.1"), ("/VOUT", "L1.2", "R6.1"),
    ("/VLOAD", "R6.4", "J2.1"),
    ("GND", "C2.2", "C1.2"), ("GND", "C1.2", "Q2.3"), ("GND", "C7.2", "J2.2"), ("GND", "C8.2", "J2.2"),
)


def min_width_mm(amps, dt):
    area_mil2 = (amps / (K_OUTER * dt ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (COPPER_MM / MIL) * MIL


def rise_c(amps, width_mm):
    area_mil2 = (width_mm / MIL) * (COPPER_MM / MIL)
    return (amps / (K_OUTER * area_mil2 ** 0.725)) ** (1 / 0.44)


def mohm_per_mm(width_mm):
    return RHO_CU / (width_mm * 1e-3 * COPPER_MM * 1e-3) * 1e-3 * 1e3


def read_board(path):
    """Pads {"J1.1": [rect, ...]} as (cx, cy, half_w, half_h, angle_rad) on F.Cu, and F.Cu tracks
    [(x0, y0, x1, y1, width, net)]. Positions in the file are footprint-relative; rotate them."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    pads = {}
    for fp in re.finditer(r'\n\t\(footprint "[^"]+"(.*?)\n\t\)(?=\n)', text, re.S):
        body = fp.group(1)
        ref = re.search(r'\(property "Reference" "([^"]+)"', body).group(1)
        at = [float(v) for v in re.search(r'^\t\t\(at ([^)]*)\)', body, re.M).group(1).split()]
        fx, fy, fa = at[0], at[1], (at[2] if len(at) > 2 else 0.0)
        for number, pos, size in re.findall(
                r'\(pad "([^"]*)" smd \w+\s+\(at ([^)]*)\)\s+\(size ([^)]*)\)', body):
            pv = [float(v) for v in pos.split()]
            px, py = pv[0], pv[1]
            pa = pv[2] if len(pv) > 2 else 0.0            # pad angle in the file is absolute
            sx, sy = (float(v) for v in size.split())
            a = math.radians(fa)
            cx = fx + px * math.cos(a) + py * math.sin(a)
            cy = fy - px * math.sin(a) + py * math.cos(a)
            pads.setdefault("%s.%s" % (ref, number), []).append((cx, cy, sx / 2, sy / 2, math.radians(pa)))
        for number, pos, size in re.findall(
                r'\(pad "([^"]*)" thru_hole \w+\s+\(at ([^)]*)\)\s+\(size ([^)]*)\)', body):
            pv = [float(v) for v in pos.split()]
            a = math.radians(fa)
            cx = fx + pv[0] * math.cos(a) + pv[1] * math.sin(a)
            cy = fy - pv[0] * math.sin(a) + pv[1] * math.cos(a)
            sx, sy = (float(v) for v in size.split())
            pads.setdefault("%s.%s" % (ref, number), []).append((cx, cy, sx / 2, sy / 2, 0.0))
    tracks = []
    for m in re.finditer(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)\s+"
                         r"\(width ([\d.]+)\)(.*?)\(net \"([^\"]+)\"\)", text, re.S):
        if '(layer "F.Cu")' in m.group(6):
            tracks.append(tuple(float(v) for v in m.groups()[:5]) + (m.group(7),))
    return pads, tracks


def pad_rect(pads, name):
    """The pad called name ("Q1.2T" = the larger pad 2)."""
    key, tab = (name[:-1], True) if name.endswith("T") else (name, False)
    rects = pads[key]
    return max(rects, key=lambda r: r[2] * r[3]) if tab else min(rects, key=lambda r: r[2] * r[3])


def seg_dist(p, a, b):
    (x, y), (x0, y0), (x1, y1) = p, a, b
    dx, dy = x1 - x0, y1 - y0
    k = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - x0 - k * dx, y - y0 - k * dy)


def touches_pad(t, rect):
    """True if track t's copper overlaps the pad rectangle: sample the centre line and test each
    point against the rectangle grown by half the track width (in the pad's own frame)."""
    cx, cy, hw, hh, ang = rect
    r = t[4] / 2
    for k in range(41):
        x = t[0] + (t[2] - t[0]) * k / 40
        y = t[1] + (t[3] - t[1]) * k / 40
        u = (x - cx) * math.cos(ang) - (y - cy) * math.sin(ang)
        v = (x - cx) * math.sin(ang) + (y - cy) * math.cos(ang)
        du, dv = max(abs(u) - hw, 0.0), max(abs(v) - hh, 0.0)
        if math.hypot(du, dv) <= r:
            return True
    return False


def joining_tracks(pads, tracks, net, a, b, min_width):
    """The shortest chain (by drawn length) of tracks on net, each at least min_width wide, joined
    end to track and running from a track touching pad a to one touching pad b; [] if none exists."""
    wide = [t for t in tracks if t[5] == net and t[4] >= min_width - 1e-9]
    ra, rb = pad_rect(pads, a), pad_rect(pads, b)

    def length(t):
        return math.hypot(t[2] - t[0], t[3] - t[1])

    def joined(t, u):
        return any(seg_dist(e, (t[0], t[1]), (t[2], t[3])) <= t[4] / 2 for e in ((u[0], u[1]), (u[2], u[3]))) or any(
            seg_dist(e, (u[0], u[1]), (u[2], u[3])) <= u[4] / 2 for e in ((t[0], t[1]), (t[2], t[3])))

    dist = {i: length(t) for i, t in enumerate(wide) if touches_pad(t, ra)}
    prev = {}
    todo = set(dist)
    done = set()
    while todo:
        i = min(todo, key=lambda k: dist[k])
        todo.discard(i)
        done.add(i)
        if touches_pad(wide[i], rb):
            chain = [i]
            while chain[-1] in prev:
                chain.append(prev[chain[-1]])
            return [wide[k] for k in chain]
        for j, u in enumerate(wide):
            if j in done or not joined(wide[i], u):
                continue
            d = dist[i] + length(u)
            if d < dist.get(j, float("inf")):
                dist[j], prev[j] = d, i
                todo.add(j)
    return []


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else PCB
    print("IPC-2221 outer layer, k = %.3f, 1 oz copper (%.0f um), allowed rise %.0f degC"
          % (K_OUTER, COPPER_MM * 1000, DT_MAX))
    for name, amps in (("rated output", I_DESIGN), ("OCP trip (60 us)", I_OCP)):
        print("  %-18s %.1f A -> minimum width %.2f mm" % (name, amps, min_width_mm(amps, DT_MAX)))
    need = min_width_mm(I_DESIGN, DT_MAX)
    pads, tracks = read_board(path)
    print("F.Cu tracks of at least %.2f mm joining the pads, in %s:" % (need, os.path.basename(path)))
    print("%-7s %-13s %7s %7s %10s %10s %9s %11s" % ("net", "pads", "length", "width", "rise 3 A",
                                                   "rise 4.5 A", "R", "drop @ 3 A"))
    ok = True
    for net, a, b in CONNECTIONS:
        chain = joining_tracks(pads, tracks, net, a, b, need)
        if not chain:
            print("%-7s %-13s none: no track of at least %.2f mm joins them" % (net, a + "-" + b, need))
            ok = False
            continue
        length = sum(math.hypot(t[2] - t[0], t[3] - t[1]) for t in chain)
        width = min(t[4] for t in chain)
        r = sum(mohm_per_mm(t[4]) * math.hypot(t[2] - t[0], t[3] - t[1]) for t in chain)
        print("%-7s %-13s %5.1f mm %5.2f mm %6.1f degC %6.1f degC %5.2f mOhm %7.1f mV"
              % (net, a + "-" + b, length, width, rise_c(I_DESIGN, width), rise_c(I_OCP, width), r,
                 r * I_DESIGN))
    print("Length and R: the shortest chain of wide tracks between the pads, pad centre to pad centre at most.")
    print("GND rows are the drawn return alone; both GND pours run in parallel with them.")
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
