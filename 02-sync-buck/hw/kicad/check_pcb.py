#!/usr/bin/env python3
"""Run the KiCad checks of the schematic and the board, the layout checks a reviewer asks for, and
show that each check fails on a sabotaged copy of the board.

Run it with KiCad 10's Python, like gen_pcb.py:  <KiCad python3> hw/kicad/check_pcb.py

1. ERC on the schematic, and check_netlist.py.
2. DRC with schematic parity, all severities, on the committed board: as saved and with
   --refill-zones (which only proves the saved fills are current; the file is not rewritten).
3. trace_width.py: IPC-2221 widths, and wide copper joining every pair of power pads.
4. Layout checks (measure()), with the limits in LIMITS (each with its basis, fixed before the board was measured):
   hot loop   the area enclosed by C1 -> VIN copper -> Q1 tab -> Q1 source -> SW copper -> Q2 tab ->
              Q2 source -> GND copper -> C1, as a polygon through the pad centres and every bend of
              the drawn tracks; GND vias beside C1, C2 and Q2's source pads; the B.Cu pour under it
   gate drive HO gate loop: the polygon Q1 gate -> GH tracks -> R1 -> HO tracks -> U1 pin 3 -> (pin 4) ->
              HS tracks -> HS via beside Q1's source -> (through the SW copper) Q1 source -> (across the FET)
              Q1 gate, through every bend and via of the drawn copper, and its area; GH and HS run as a pair on
              B.Cu, so the largest gap between them and the share of GH that lies under F.Cu SW copper (sampled
              on the real copper) are measured too; the LO path length and the pin-to-gate distances
   bootstrap  C5 at U1 pins 2 and 4: pin-to-pad distances, and the area of the loop U1 pin 2 -> HB -> C5 ->
              HS -> U1 pin 4
   Kelvin     the SH_P/SH_N copper touches R6's sense pads 2/3 only, never pad 1/4 or other VOUT/VLOAD
              copper; the loop the two sense lines enclose with the shunt, through every bend and via
   analog     every VSENSE, ISENSE and VINSENSE track stays clear of the switch-node copper and of L1
   power      no VIN/SW/VLOAD/GND track narrower than POWER_MIN inside its rule area, no VOUT track
5. Sabotaged copies of the board, each written in its own temporary directory, which the checks
   must reject (CASES below).
Prints PASS/FAIL per check; exit 0 only if every check passes.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pcbnew

from gen_pcb import HERE, KICAD_CLI, ORIGIN, PCB, POWER_MIN, PROJECT, RULE_AREAS, SCH
import trace_width as tw

PROJECT_FILES = [PROJECT + ext for ext in (".kicad_pro", ".kicad_dru", ".kicad_sch", ".kicad_sym")] + [
    "sym-lib-table", "fp-lib-table"]
# Each limit is a target fixed before this board was measured, with its basis stated here.
LIMITS = dict(
    loop_mm2=63.0,            # hot loop through C1: 1.5 x the floor set by the four FET pad centres alone
                              # (tab, source, tab, source of two DPAKs side by side: 42 mm^2, logged as
                              # "FET floor"); the 1.5 allows for the C1 pad and the 2 mm power tracks
    via_reach_mm=1.5,         # a GND via within this distance of each hot-loop ground pad (about two via
                              # diameters of pour-to-pad detour at 0.8 mm vias)
    pour_cover=0.95,          # share of the hot-loop polygon with B.Cu GND pour under it (the return image)
    lo_pin_to_gate_mm=6.0,    # U1 LO pin to Q2 gate pad; the Miller-critical low side. 6 mm of track is about
                              # 6 nH at the usual 1 nH/mm; with R2 2.2 ohm the loop's L/R is ~3 ns, well
                              # under one 12.5 ns PWM timer tick
    ho_pin_to_gate_mm=18.0,   # U1 HO pin to Q1 gate pad: one third of the 54 mm board width. Q1 sits where
                              # the hot loop needs it, so the driver is allowed to be further off than for LO
    lo_loop_mm=10.0,          # drawn LO + GL path, U1 pin 8 to Q2 gate: 10 nH at 1 nH/mm
    ho_loop_mm2=30.0,         # HO gate loop area, plan view: about half of the hot-loop limit; the gate loop
                              # carries 12 V / 2.2 ohm = 5.5 A peak against the 3 A of the power loop but for
                              # far less time, so it gets the smaller budget
    lane_gap_mm=1.5,          # largest gap between the GH and HS tracks over the B.Cu lane: 1.5 mm is
                              # about three 0.5 mm track widths, still a closely coupled pair
    boot_mm2=10.0,            # bootstrap loop U1 pin 2 -> C5 -> pin 4: one third of the HO gate-loop limit
    boot_pin_to_cap_mm=3.0,   # C5 pad to its U1 pin: two 0603 pad pitches
    analog_clear_mm=5.0,      # analog tracks to any SW copper and to L1's courtyard
)
results = []


def check(name, ok, detail):
    results.append(ok)
    print("%s  %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


# ----------------------------------------------------------------- measurements on one board file
def mmv(v):
    return pcbnew.ToMM(v)


def xy(v):
    return (mmv(v.x) - ORIGIN[0], mmv(v.y) - ORIGIN[1])


def V(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(ORIGIN[0] + x), pcbnew.FromMM(ORIGIN[1] + y))


def shoelace(pts):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2


def ordered_path(chain, start, end):
    """Polyline from pad centre start through every track of chain (in order) to pad centre end."""
    pts = [start]
    for t in chain:
        a, b = (t[0], t[1]), (t[2], t[3])
        if math.dist(b, pts[-1]) < math.dist(a, pts[-1]):
            a, b = b, a
        pts += [a, b]
    pts.append(end)
    out = [pts[0]]
    for q in pts[1:]:
        if math.dist(q, out[-1]) > 1e-6:
            out.append(q)
    return out


def hot_loop(pads, tracks, cap):
    """Polygon of the hot loop through input capacitor cap (C1 or C2), and the drawn hops."""
    need = tw.min_width_mm(tw.I_DESIGN, tw.DT_MAX)
    centre = {n: tw.pad_rect(pads, n)[:2] for n in (cap + ".1", cap + ".2", "Q1.2T", "Q1.3", "Q2.2T", "Q2.3")}
    poly = []
    for net, a, b in (("/VIN", cap + ".1", "Q1.2T"), ("/SW", "Q1.3", "Q2.2T"), ("GND", "Q2.3", cap + ".2")):
        chain = tw.joining_tracks(pads, tracks, net, a, b, need)
        if not chain:
            return None
        chain = list(reversed(chain))         # joining_tracks lists them from b back to a
        poly += ordered_path(chain, centre[a], centre[b])
    return poly


def key(p):
    return (round(p[0], 3), round(p[1], 3))


def graph_path(items, nets, a, b, max_width=None, tol=0.02):
    """Shortest polyline along the drawn tracks of nets (any layer; a via joins the tracks that end on it)
    from point a to point b, in board mm; None if the copper does not join them."""
    edges = {}
    for t in items:
        if t.GetClass() != "PCB_TRACK" or t.GetNetname() not in nets:
            continue
        if max_width is not None and mmv(t.GetWidth()) > max_width + 1e-9:
            continue
        p, q = xy(t.GetStart()), xy(t.GetEnd())
        w = math.dist(p, q)
        edges.setdefault(key(p), []).append((key(q), w))
        edges.setdefault(key(q), []).append((key(p), w))

    def near(pt):
        best = min(edges, key=lambda k: math.dist(k, pt), default=None)
        return best if best is not None and math.dist(best, pt) <= tol else None

    src, dst = near(a), near(b)
    if src is None or dst is None:
        return None
    dist, prev, todo = {src: 0.0}, {}, {src}
    while todo:
        n = min(todo, key=lambda k: dist[k])
        todo.discard(n)
        if n == dst:
            break
        for m, w in edges[n]:
            if dist[n] + w < dist.get(m, float("inf")) - 1e-12:
                dist[m], prev[m] = dist[n] + w, n
                todo.add(m)
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    return list(reversed(path))


def plength(path):
    return sum(math.dist(p, q) for p, q in zip(path, path[1:]))


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def crossings(poly):
    """Crossing points of a closed polygon's non-adjacent edges: [(i, j, (x, y))] for edges i < j."""
    n = len(poly)
    found = []
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            a, b, c, d = poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]
            if _cross(a, b, c) * _cross(a, b, d) < 0 and _cross(c, d, a) * _cross(c, d, b) < 0:
                k = _cross(c, d, a) / (_cross(c, d, a) - _cross(c, d, b))
                found.append((i, j, (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)))
    return found


def lobes(poly):
    """(total unsigned area, [lobe areas]) of a closed polygon with at most one self-crossing; (nan, []) if more.
    A crossing makes a figure of eight: the area counted is the sum of the two lobes, not their difference."""
    x = crossings(poly)
    if not x:
        return shoelace(poly), [shoelace(poly)]
    if len(x) > 1:
        return float("nan"), []
    i, j, pt = x[0]
    one = [pt] + poly[i + 1:j + 1]
    two = [pt] + poly[j + 1:] + poly[:i + 1]
    areas = [shoelace(one), shoelace(two)]
    return sum(areas), areas


def loop_polygon(parts):
    """Join polylines end to end into one closed polygon, dropping repeated points."""
    out = []
    for part in parts:
        for q in part:
            if not out or math.dist(q, out[-1]) > 1e-6:
                out.append(q)
    if math.dist(out[0], out[-1]) < 1e-6:
        out.pop()
    return out


def seg_gap(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    k = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - a[0] - k * dx, p[1] - a[1] - k * dy)


def samples(t, step=0.05):
    p, q = xy(t.GetStart()), xy(t.GetEnd())
    n = max(1, int(math.dist(p, q) / step))
    return [(p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n) for k in range(n + 1)]


def track_items(board):
    items = board.Tracks()
    return [items[i] for i in range(len(items))]


def pad_polys(board):
    out = []
    fps = board.Footprints()
    for i in range(len(fps)):
        for p in fps[i].Pads():
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                if p.IsOnLayer(layer):
                    ps = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(ps, layer, 0, pcbnew.FromMM(0.005), pcbnew.ERROR_INSIDE)
                    out.append((fps[i].GetReference(), p.GetNumber(), p.GetNetname(), layer, ps))
    return out


def item_poly(item, layer):
    ps = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(ps, layer, 0, pcbnew.FromMM(0.005), pcbnew.ERROR_INSIDE)
    return ps


def poly_gap(a, b):
    """Edge-to-edge distance in mm between two SHAPE_POLY_SETs (0 if they touch or overlap)."""
    inter = pcbnew.SHAPE_POLY_SET(a)
    inter.BooleanIntersection(b)
    if inter.Area() > 0:
        return 0.0
    best = float("inf")
    for src, dst in ((a, b), (b, a)):
        for k in range(src.OutlineCount()):
            o = src.Outline(k)
            for j in range(o.PointCount()):
                best = min(best, math.sqrt(dst.SquaredDistance(o.CPoint(j))))
    return mmv(int(best))


def measure(path):
    board = pcbnew.LoadBoard(path)
    pads, tracks = tw.read_board(path)
    fps = {f.GetReference(): f for f in board.Footprints()}
    items = track_items(board)
    allpads = pad_polys(board)
    out = {}

    # hot loop
    loops = {cap: hot_loop(pads, tracks, cap) for cap in ("C1", "C2")}
    if loops["C1"] is None:
        out["hot loop area"] = (False, "no wide drawn path round the loop")
    else:
        area = {cap: shoelace(p) for cap, p in loops.items() if p}
        floor = shoelace([tw.pad_rect(pads, n)[:2] for n in ("Q1.2T", "Q1.3", "Q2.2T", "Q2.3")])
        lp = loops["C1"]
        per = sum(math.dist(p, q) for p, q in zip(lp, lp[1:] + lp[:1]))
        out["hot loop area"] = (area["C1"] <= LIMITS["loop_mm2"],
                                "%.1f mm^2 through C1 (limit %.0f), %.1f mm^2 through C2, FET floor %.1f mm^2; "
                                "perimeter %.1f mm, about %.0f nH at the 1 nH/mm rule of thumb (a 2 mm strip 1.6 mm over a "
                                "plane gives the same 1 nH/mm): an upper-end order-of-magnitude estimate, not an extraction; "
                                "polygon %s" % (area["C1"], LIMITS["loop_mm2"], area.get("C2", float("nan")), floor,
                                                            per, per,
                                                            " ".join("(%.2f,%.2f)" % (x - ORIGIN[0], y - ORIGIN[1])
                                                                     for x, y in loops["C1"])))
        poly = pcbnew.SHAPE_POLY_SET()
        poly.NewOutline()
        for x, y in loops["C1"]:               # trace_width.py coordinates are absolute sheet mm
            poly.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        pour = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetLayer() == pcbnew.B_Cu][0]
        covered = pcbnew.SHAPE_POLY_SET(poly)
        covered.BooleanIntersection(pour.GetFilledPolysList(pcbnew.B_Cu))
        share = covered.Area() / poly.Area()
        out["B.Cu GND pour under the hot loop"] = (share >= LIMITS["pour_cover"],
                                                   "%.1f %% of the loop polygon (limit %.0f %%)"
                                                   % (100 * share, 100 * LIMITS["pour_cover"]))
    gvias = [t for t in items if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"]
    counts = []
    for ref, num in (("C1", "2"), ("C2", "2"), ("Q2", "3")):
        ps = [p for (r, n, _net, layer, p) in allpads if r == ref and n == num and layer == pcbnew.F_Cu][0]
        reach = pcbnew.FromMM(LIMITS["via_reach_mm"])
        counts.append("%s.%s %d" % (ref, num, sum(1 for v in gvias if ps.SquaredDistance(v.GetPosition()) <=
                                                  (reach + v.GetWidth() // 2) ** 2)))
    out["GND vias at the hot-loop ground pads"] = (all(not c.endswith(" 0") for c in counts),
                                                  "vias within %.1f mm: %s" % (LIMITS["via_reach_mm"],
                                                                               ", ".join(counts)))

    # gate drive
    def pad_xy(ref, num, big=False):
        ps = [p for p in fps[ref].Pads() if p.GetNumber() == num]
        p = max(ps, key=lambda q: q.GetSize(pcbnew.F_Cu).x * q.GetSize(pcbnew.F_Cu).y) if big else min(
            ps, key=lambda q: q.GetSize(pcbnew.F_Cu).x * q.GetSize(pcbnew.F_Cu).y)
        return xy(p.GetPosition())

    lo_d = math.dist(pad_xy("U1", "8"), pad_xy("Q2", "1"))
    ho_d = math.dist(pad_xy("U1", "3"), pad_xy("Q1", "1"))
    out["U1 next to the FETs"] = (lo_d <= LIMITS["lo_pin_to_gate_mm"] and ho_d <= LIMITS["ho_pin_to_gate_mm"],
                                  "LO pin -> Q2 gate %.1f mm (limit %.0f), HO pin -> Q1 gate %.1f mm (limit %.0f)"
                                  % (lo_d, LIMITS["lo_pin_to_gate_mm"], ho_d, LIMITS["ho_pin_to_gate_mm"]))
    lo_a = graph_path(items, ("/LO",), pad_xy("U1", "8"), pad_xy("R2", "1"))
    lo_b = graph_path(items, ("/GL",), pad_xy("R2", "2"), pad_xy("Q2", "1"))
    lo_len = plength(lo_a) + plength(lo_b) if lo_a and lo_b else float("nan")
    out["LO gate path length"] = (lo_len <= LIMITS["lo_loop_mm"],
                                  "U1 pin 8 -> R2 -> Q2 gate %.1f mm of drawn track (limit %.0f)"
                                  % (lo_len, LIMITS["lo_loop_mm"]))

    # copper helpers for the HO gate loop
    sw_f = pcbnew.SHAPE_POLY_SET()
    for (_r, _n, net, layer, p) in allpads:
        if net == "/SW" and layer == pcbnew.F_Cu:
            sw_f.BooleanAdd(p)
    for t in items:
        if t.GetNetname() == "/SW" and t.GetClass() == "PCB_TRACK" and t.GetLayer() == pcbnew.F_Cu:
            sw_f.BooleanAdd(item_poly(t, pcbnew.F_Cu))
    hs_vias = [xy(v.GetPosition()) for v in items if v.GetClass() == "PCB_VIA" and v.GetNetname() == "/SW"]
    src = pad_xy("Q1", "3")
    hs_end = min(hs_vias, key=lambda v: math.dist(v, src)) if hs_vias else None
    gh_p = graph_path(items, ("/GH",), pad_xy("Q1", "1"), pad_xy("R1", "2"))
    ho_p = graph_path(items, ("/HO",), pad_xy("R1", "1"), pad_xy("U1", "3"))
    hs_p = graph_path(items, ("/SW",), pad_xy("U1", "4"), hs_end, max_width=0.6) if hs_end else None
    if not (gh_p and ho_p and hs_p):
        out["HO gate loop area"] = (False, "the GH, HO and HS copper does not join Q1 gate, R1, U1 pins 3/4 and an HS via")
        out["GH/HS lane"] = (False, "no lane")
    else:
        poly = loop_polygon([gh_p, ho_p, [pad_xy("U1", "4")], hs_p, [src]])
        area_ho, lobe_list = lobes(poly)
        crossing = len(crossings(poly))
        n_vias = sum(1 for v in items if v.GetClass() == "PCB_VIA" and v.GetNetname() in ("/GH", "/HO", "/SW")
                     and any(math.dist(xy(v.GetPosition()), q) < 0.01 for q in poly))
        out["HO gate loop area"] = (area_ho <= LIMITS["ho_loop_mm2"],
                                    "%.1f mm^2 (limit %.0f)%s; path GH+R1+HO %.1f mm, HS %.1f mm, %d vias; HS via %.1f mm "
                                    "from Q1's source pad; polygon %s"
                                    % (area_ho, LIMITS["ho_loop_mm2"],
                                       " = lobes %s (one crossing, on different layers)" % " + ".join("%.1f" % l for l in lobe_list)
                                       if crossing else "", plength(gh_p) + plength(ho_p), plength(hs_p), n_vias,
                                       math.dist(hs_end, src), " ".join("(%.2f,%.2f)" % q for q in poly)))
        gh_b = [t for t in items if t.GetClass() == "PCB_TRACK" and t.GetNetname() == "/GH" and t.GetLayer() == pcbnew.B_Cu]
        hs_b = [t for t in items if t.GetClass() == "PCB_TRACK" and t.GetNetname() == "/SW" and t.GetLayer() == pcbnew.B_Cu
                and t.GetWidth() <= pcbnew.FromMM(0.6)]
        gh_segs = [(xy(t.GetStart()), xy(t.GetEnd())) for t in gh_b]
        gap = max((min(seg_gap(q, a_, b_) for a_, b_ in gh_segs) for t in hs_b for q in samples(t)), default=float("nan"))
        under = total = 0
        for t in gh_b:
            for q in samples(t):
                total += 1
                under += 1 if sw_f.Contains(V(*q)) else 0
        gh_len = sum(mmv(t.GetLength()) for t in gh_b)
        hs_len = sum(mmv(t.GetLength()) for t in hs_b)
        via_in_sw = sw_f.Contains(V(*hs_end))
        out["GH/HS lane"] = (gap <= LIMITS["lane_gap_mm"] and via_in_sw and gh_len > 0,
                             "GH %.1f mm and HS %.1f mm side by side on B.Cu, largest gap %.2f mm (limit %.1f); %.1f of "
                             "the %.1f mm of GH lies under F.Cu SW copper (sampled on the copper); HS via on SW copper: %s"
                             % (gh_len, hs_len, gap, LIMITS["lane_gap_mm"], gh_len * under / max(total, 1), gh_len,
                                "yes" if via_in_sw else "NO"))
    # bootstrap capacitor at the pins
    hb_p = graph_path(items, ("/HB",), pad_xy("U1", "2"), pad_xy("C5", "1"))
    hsb_p = graph_path(items, ("/SW",), pad_xy("C5", "2"), pad_xy("U1", "4"), max_width=0.6)
    d1 = math.dist(pad_xy("U1", "2"), pad_xy("C5", "1"))
    d2 = math.dist(pad_xy("U1", "4"), pad_xy("C5", "2"))
    if not (hb_p and hsb_p):
        out["bootstrap loop at the pins"] = (False, "C5 is not joined to U1 pins 2 and 4 by drawn copper (C5.1 -> pin 2 %.1f mm, "
                                              "C5.2 -> pin 4 %.1f mm, limit %.1f)" % (d1, d2, LIMITS["boot_pin_to_cap_mm"]))
    else:
        bp = loop_polygon([hb_p, [pad_xy("C5", "2")], hsb_p, [pad_xy("U1", "2")]])
        area_b = lobes(bp)[0]
        out["bootstrap loop at the pins"] = (
            area_b <= LIMITS["boot_mm2"] and max(d1, d2) <= LIMITS["boot_pin_to_cap_mm"],
            "%.1f mm^2 (limit %.0f); C5.1 -> U1 pin 2 %.1f mm, C5.2 -> pin 4 %.1f mm (limit %.1f); drawn HB %.1f mm, HS %.1f mm"
            % (area_b, LIMITS["boot_mm2"], d1, d2, LIMITS["boot_pin_to_cap_mm"], plength(hb_p), plength(hsb_p)))

    # Kelvin sense
    bad = []
    r6 = {n: p for (r, n, _net, layer, p) in allpads if r == "R6" and layer == pcbnew.F_Cu}
    own = {"/SH_P": "2", "/SH_N": "3"}
    force = [(r, n, layer, p) for (r, n, net, layer, p) in allpads if net in ("/VOUT", "/VLOAD")]
    force_tracks = [t for t in items if t.GetNetname() in ("/VOUT", "/VLOAD")]
    sense_len = {}
    for t in items:
        net = t.GetNetname()
        if net not in own:
            continue
        if t.GetClass() == "PCB_TRACK":
            sense_len[net] = sense_len.get(net, 0.0) + mmv(t.GetLength())
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if not t.IsOnLayer(layer):
                continue
            mine = item_poly(t, layer)
            for num, p in r6.items():
                if layer == pcbnew.F_Cu and num != own[net] and poly_gap(mine, p) <= 0.0:
                    bad.append("%s touches R6 pad %s" % (net, num))
            for r, n, fl, p in force:
                if fl == layer and r != "R6" and poly_gap(mine, p) <= 0.0:
                    bad.append("%s touches %s.%s" % (net, r, n))
            for f in force_tracks:
                if f.IsOnLayer(layer) and poly_gap(mine, item_poly(f, layer)) <= 0.0:
                    bad.append("%s touches %s copper" % (net, f.GetNetname()))
    touched = {net: sorted(n for num_net in [net] for n, p in r6.items()
                           if any(poly_gap(item_poly(t, pcbnew.F_Cu), p) <= 0.0 for t in items
                                  if t.GetNetname() == net and t.IsOnLayer(pcbnew.F_Cu)))
               for net in own}
    kelvin = None
    detour = None
    if all(touched[n] == [own[n]] for n in own):
        sp_ = graph_path(items, ("/SH_P",), pad_xy("R6", "2"), pad_xy("U2", "3"))
        sn_ = graph_path(items, ("/SH_N",), pad_xy("R6", "3"), pad_xy("U2", "4"))
        if sp_ and sn_:
            # R6.2 -> SH_P -> U2.3, U2.4 -> SH_N -> R6.3, then back through the shunt element: pad 4 end, pad 1 end
            elem = [pad_xy("R6", "4"), pad_xy("R6", "1"), pad_xy("R6", "2")]
            kp = loop_polygon([sp_, list(reversed(sn_)), elem])
            kelvin = lobes(kp)[0]
        # the same two lines kept on F.Cu: SH_P cannot run straight (it would cross force pad 1), so it goes round
        # that pad's west side, 1.0 mm clear, and meets U2 pin 3 from the north
        p1 = pad_xy("R6", "1")
        west = p1[0] - 1.145 - 1.0
        detour = lobes([pad_xy("R6", "2"), (west, pad_xy("R6", "2")[1]), (west, pad_xy("U2", "3")[1] + 0.65),
                        (pad_xy("U2", "3")[0], pad_xy("U2", "3")[1] + 0.65), pad_xy("U2", "3"), pad_xy("U2", "4"),
                        pad_xy("R6", "3"), pad_xy("R6", "4"), pad_xy("R6", "1")])[0]
    out["Kelvin sense on pads 2/3 only"] = (
        not bad and all(touched[n] == [own[n]] for n in own),
        ("SH_P on R6 pad %s, SH_N on pad %s; SH_P %.1f mm, SH_N %.1f mm; sense loop with the shunt element (drawn path, "
         "through every bend and via; SH_P crosses the element once, so the two lobes are added) %.1f mm^2; the same lines all on F.Cu would enclose %.1f mm^2"
         % ("/".join(touched["/SH_P"]) or "-", "/".join(touched["/SH_N"]) or "-",
            sense_len.get("/SH_P", 0), sense_len.get("/SH_N", 0), kelvin if kelvin is not None else float("nan"),
            detour if detour is not None else float("nan")))
        + ("; " + "; ".join(sorted(set(bad))) if bad else ""))

    # analog clear of the switch node and L1
    sw = [p for (r, n, net, layer, p) in allpads if net == "/SW"] + [
        item_poly(t, layer) for t in items if t.GetNetname() == "/SW" for layer in (pcbnew.F_Cu, pcbnew.B_Cu)
        if t.IsOnLayer(layer)]
    l1 = fps["L1"].GetCourtyard(pcbnew.F_CrtYd)
    worst = (float("inf"), "")
    for t in items:
        net = t.GetNetname()
        if net not in ("/VSENSE", "/ISENSE", "/VINSENSE") or t.GetClass() != "PCB_TRACK":
            continue
        mine = item_poly(t, t.GetLayer())
        d = min([poly_gap(mine, s) for s in sw] + [poly_gap(mine, l1)])
        if d < worst[0]:
            worst = (d, net)
    out["analog tracks clear of SW and L1"] = (worst[0] >= LIMITS["analog_clear_mm"],
                                              "closest: %s %.1f mm (limit %.1f)"
                                              % (worst[1], worst[0], LIMITS["analog_clear_mm"]))

    # power widths inside the rule areas
    narrow = []
    areas = {z.GetZoneName(): z for z in board.Zones() if z.GetIsRuleArea()}
    for name, net, _pts in RULE_AREAS:
        outline = areas[name].Outline()
        for t in items:
            if (t.GetClass() == "PCB_TRACK" and t.GetNetname() == net and t.GetWidth() < pcbnew.FromMM(POWER_MIN)
                    and (net != "/SW" or t.GetLayer() == pcbnew.F_Cu)):       # the SW rule is F.Cu only
                if poly_gap(item_poly(t, t.GetLayer()), outline) <= 0.0:
                    narrow.append("%s %.2f mm in %s" % (net, mmv(t.GetWidth()), name))
    for t in items:
        if t.GetClass() == "PCB_TRACK" and t.GetNetname() == "/VOUT" and t.GetWidth() < pcbnew.FromMM(POWER_MIN):
            narrow.append("/VOUT %.2f mm" % mmv(t.GetWidth()))
    out["power copper at least POWER_MIN"] = (not narrow, "%s (POWER_MIN %.1f mm)"
                                             % ("; ".join(narrow) or "none narrower", POWER_MIN))
    return out


# ----------------------------------------------------------------- DRC, sabotage
def drc(board_path, *extra):
    """kicad-cli DRC with schematic parity; returns (exit code, counts, report text)."""
    report = os.path.join(os.path.dirname(board_path), "drc.rpt")
    r = subprocess.run([KICAD_CLI, "pcb", "drc", "--schematic-parity", "--severity-all",
                        "--exit-code-violations"] + list(extra) + ["-o", report, board_path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with open(report, encoding="utf-8") as f:
        text = f.read()
    counts = [int(n) for n in re.findall(r"\*\* Found (\d+) ", text)]   # violations, unconnected, parity
    return r.returncode, counts, text


def summary(code, counts):
    return "%d violations, %d unconnected, %d parity errors (exit %d)" % (counts[0], counts[1], counts[2], code)


def child(*args):
    r = subprocess.run([sys.executable, os.path.abspath(__file__)] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, check=True)
    return r.stdout.strip().splitlines()[-1]


def copy_project(tmp):
    for f in PROJECT_FILES:
        shutil.copy(os.path.join(HERE, f), tmp)


def tracks_of(board, net, layer=None):
    return [t for t in track_items(board) if t.GetClass() == "PCB_TRACK" and t.GetNetname() == net
            and (layer is None or t.GetLayer() == layer)]


def new_track(board, like, a, b, width):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(a)
    t.SetEnd(b)
    t.SetLayer(like.GetLayer())
    t.SetNet(like.GetNet())
    t.SetWidth(pcbnew.FromMM(width))
    board.Add(t)
    return t


def sab_loop(board):
    """Hot-loop ground taken the long way: the C1 -> Q2 source track detours 1.5 mm further north."""
    hot = [t for t in tracks_of(board, "GND", pcbnew.F_Cu) if t.GetWidth() >= pcbnew.FromMM(1.9)
           and abs(mmv(t.GetStart().y) - ORIGIN[1] - 11.05) < 0.01 and abs(mmv(t.GetEnd().y) - ORIGIN[1] - 11.05) < 0.01]
    for t in hot:
        a, b = t.GetStart(), t.GetEnd()
        up = pcbnew.FromMM(1.5)
        new_track(board, t, a, pcbnew.VECTOR2I(a.x, a.y - up), 2.0)
        new_track(board, t, pcbnew.VECTOR2I(a.x, a.y - up), pcbnew.VECTOR2I(b.x, b.y - up), 2.0)
        new_track(board, t, pcbnew.VECTOR2I(b.x, b.y - up), b, 2.0)
        board.Delete(t)
    return "hot-loop GND track C1 -> Q2 source detoured 1.5 mm north (%d segment)" % len(hot)


def sab_vias(board):
    fps = {f.GetReference(): f for f in board.Footprints()}
    gone = 0
    for ref, num in (("C1", "2"), ("C2", "2"), ("Q2", "3")):
        p = [q for q in fps[ref].Pads() if q.GetNumber() == num][0]
        for v in [t for t in track_items(board) if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"]:
            if math.dist(xy(v.GetPosition()), xy(p.GetPosition())) < 3.2:
                board.Delete(v)
                gone += 1
    return "GND vias at C1, C2 and Q2 source removed (%d)" % gone


def sab_driver(board):
    u1 = [f for f in board.Footprints() if f.GetReference() == "U1"][0]
    u1.Move(pcbnew.VECTOR2I(pcbnew.FromMM(1.5), pcbnew.FromMM(1.0)))
    return "U1 moved 1.5 mm east, 1.0 mm south"


def sab_kelvin(board):
    fps = {f.GetReference(): f for f in board.Footprints()}
    p3 = [q for q in fps["R6"].Pads() if q.GetNumber() == "3"][0].GetPosition()
    p4 = [q for q in fps["R6"].Pads() if q.GetNumber() == "4"][0].GetPosition()
    for t in tracks_of(board, "/SH_N", pcbnew.F_Cu):
        if t.GetStart() == p3:
            t.SetStart(p4)
        if t.GetEnd() == p3:
            t.SetEnd(p4)
    return "SH_N track moved from R6 sense pad 3 to force pad 4"


def sab_analog(board):
    t = tracks_of(board, "/VSENSE", pcbnew.F_Cu)[0]
    new_track(board, t, V(16.2, 21.5), V(16.2, 24.0), 0.25)       # free copper beside L1 pad 1
    return "a VSENSE track added 1.2 mm from L1 pad 1 (SW)"


def sab_narrow(net, width, area=None):
    def change(board):
        hits = [t for t in tracks_of(board, net) if t.GetWidth() >= pcbnew.FromMM(1.9)]
        if area:
            z = [z for z in board.Zones() if z.GetZoneName() == area][0].Outline()
            hits = [t for t in hits if poly_gap(item_poly(t, t.GetLayer()), z) <= 0.0]
        for t in hits:
            t.SetWidth(pcbnew.FromMM(width))
        return "%s power tracks %s-> %.1f mm (%d)" % (net.strip("/"), "in %s " % area if area else "", width,
                                                      len(hits))
    return change


def sab_redraw_gnd(board):
    z = [z for z in board.Zones() if z.GetZoneName() == "GND_HOT"][0].Outline()
    old = [t for t in tracks_of(board, "GND", pcbnew.F_Cu) if t.GetWidth() >= pcbnew.FromMM(1.9)
           and poly_gap(item_poly(t, t.GetLayer()), z) <= 0.0]
    for t in old:
        new_track(board, t, t.GetStart(), t.GetEnd(), 0.5)
        board.Delete(t)
    return "hot-loop GND redrawn as new 0.5 mm tracks (%d)" % len(old)


def sab_unconnected(board):
    old = tracks_of(board, "/HB")
    for t in old:
        board.Delete(t)
    return "HB tracks removed (%d)" % len(old)


def sab_parity(board):
    [f for f in board.Footprints() if f.GetReference() == "R5"][0].SetValue("3.3R")
    return "R5 value 1.8R -> 3.3R"


def move_vertex(board, net, layer, old, new):
    """Move every end of a track of net on layer that sits at old (board mm) to new; returns how many."""
    n = 0
    for t in tracks_of(board, net, layer):
        for get, put in ((t.GetStart, t.SetStart), (t.GetEnd, t.SetEnd)):
            if math.dist(xy(get()), old) < 0.01:
                put(V(*new))
                n += 1
    return n


def sab_pour(board):
    """A 1.0 mm B.Cu track (net VIN) across the hot loop, zones refilled: the pour is cut round it."""
    like = tracks_of(board, "/VIN", pcbnew.B_Cu)[0]
    new_track(board, like, V(16.0, 14.3), V(32.0, 14.3), 1.0)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return "1.0 mm B.Cu track across the hot loop at y = 14.3, pour refilled"


def sab_gateloop(board):
    n = move_vertex(board, "/GH", pcbnew.B_Cu, (18.99, 19.81), (18.2, 21.8))
    return "GH lane corner (18.99,19.81) moved 2.1 mm south-west (%d track ends)" % n


def sab_lane(board):
    n = move_vertex(board, "/GH", pcbnew.B_Cu, (25.84, 26.71), (25.0, 27.5))
    return "GH lane corner (25.84,26.71) moved 1.2 mm south-west, GH 2.1 mm from HS (%d track ends)" % n


def sab_boot(board):
    c5 = [f for f in board.Footprints() if f.GetReference() == "C5"][0]
    c5.Move(pcbnew.VECTOR2I(pcbnew.FromMM(-2.0), 0))
    return "C5 moved 2 mm west of its pads (pin-to-cap 2.3 -> 4.3 mm)"


def sab_stale(board):
    n = move_vertex(board, "/GH", pcbnew.B_Cu, (25.84, 26.71), (24.3, 26.71))
    return "GH lane corner (25.84,26.71) moved 1.5 mm west, zones not refilled (%d track ends)" % n


CASES = {"loop": sab_loop, "vias": sab_vias, "driver": sab_driver, "kelvin": sab_kelvin, "analog": sab_analog,
         "sw": sab_narrow("/SW", 1.0, "PWR_SW"), "vin": sab_narrow("/VIN", 1.0, "PWR_VIN"),
         "vout": sab_narrow("/VOUT", 1.0), "gnd": sab_redraw_gnd, "unconnected": sab_unconnected,
         "parity": sab_parity, "pour": sab_pour, "gateloop": sab_gateloop, "lane": sab_lane, "boot": sab_boot,
         "stale": sab_stale}
# case: (measure() check that must fail or None, DRC expectation or None, trace_width.py must fail)
EXPECT = {
    "loop": ("hot loop area", None, False),
    "vias": ("GND vias at the hot-loop ground pads", None, False),
    "driver": ("U1 next to the FETs", None, False),
    "kelvin": ("Kelvin sense on pads 2/3 only", None, False),
    "analog": ("analog tracks clear of SW and L1", None, False),
    "sw": (None, ("rule 'Switch-node F.Cu", "SW width rule"), True),
    "vin": (None, ("rule 'VIN power copper", "VIN width rule"), True),
    "vout": (None, ("rule 'Every VOUT track", "VOUT width rule"), True),
    "gnd": ("power copper at least POWER_MIN", ("rule 'Hot-loop ground", "GND_HOT width rule"), True),
    "unconnected": (None, ("unconnected", "unconnected pad"), False),
    "parity": (None, ("parity", "parity error"), False),
    "pour": ("B.Cu GND pour under the hot loop", None, False),
    "gateloop": ("HO gate loop area", None, False),
    "lane": ("GH/HS lane", None, False),
    "boot": ("bootstrap loop at the pins", None, False),
    "stale": (None, ("clearance", "clearance to the stale pour"), False),
}


def main():
    version = subprocess.run([KICAD_CLI, "version"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print("KiCad %s, board %s" % (version, os.path.relpath(PCB, os.path.join(HERE, "..", ".."))))
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "erc.rpt")
        r = subprocess.run([KICAD_CLI, "sch", "erc", "--exit-code-violations", "-o", report, SCH],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(report, encoding="utf-8") as f:
            line = re.search(r"\*\* ERC messages: .*", f.read()).group(0)
        check("ERC", r.returncode == 0 and "Errors 0  Warnings 0" in line, line.strip("* ") + " (exit %d)" % r.returncode)
    r = subprocess.run([sys.executable, os.path.join(HERE, "check_netlist.py")], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, env=dict(os.environ, KICAD_CLI=KICAD_CLI))
    check("check_netlist.py", r.returncode == 0 and "RESULT: PASS" in r.stdout, r.stdout.strip().splitlines()[-1])

    with tempfile.TemporaryDirectory() as tmp:
        copy_project(tmp)
        shutil.copy(PCB, tmp)
        board = os.path.join(tmp, PROJECT + ".kicad_pcb")
        code, counts, _ = drc(board)
        check("DRC, board as committed", code == 0 and counts == [0, 0, 0], summary(code, counts))
        code, counts, _ = drc(board, "--refill-zones")
        check("DRC with --refill-zones", code == 0 and counts == [0, 0, 0], summary(code, counts))
    r = subprocess.run([sys.executable, os.path.join(HERE, "trace_width.py")], stdout=subprocess.PIPE, text=True)
    check("trace_width.py", r.returncode == 0, r.stdout.strip().splitlines()[-1])
    for name, (ok, detail) in json.loads(child("--measure", PCB)).items():
        check(name, ok, detail)

    print("Sabotaged copies (each check must reject its case):")
    for case in CASES:
        must_fail, drc_expect, width_check = EXPECT[case]
        with tempfile.TemporaryDirectory() as tmp:
            copy_project(tmp)
            path = os.path.join(tmp, PROJECT + ".kicad_pcb")
            what = child("--sabotage", case, PCB, path)
            if must_fail:
                ok, detail = json.loads(child("--measure", path))[must_fail]
                check("%s rejects: %s" % (must_fail, what), not ok, detail)
            if drc_expect:
                key, label = drc_expect
                code, counts, text = drc(path)
                seen = {"unconnected": counts[1] >= 1, "parity": counts[2] >= 1}.get(key, key in text)
                check("DRC rejects: " + what, code != 0 and seen,
                      "%s; %s%s reported" % (summary(code, counts), "" if seen else "no ", label))
            if width_check:
                r = subprocess.run([sys.executable, os.path.join(HERE, "trace_width.py"), path],
                                   stdout=subprocess.PIPE, text=True)
                check("trace_width.py rejects: " + what, r.returncode != 0,
                      "%s (exit %d)" % (r.stdout.strip().splitlines()[-1], r.returncode))
    # the schematic checks: one net label renamed in a copy of the project
    with tempfile.TemporaryDirectory() as tmp:
        copy_project(tmp)
        sch = os.path.join(tmp, PROJECT + ".kicad_sch")
        with open(sch, encoding="utf-8") as f:
            text = f.read()
        text = text.replace('(label "VSENSE"', '(label "VSENSX"', 1)
        with open(sch, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        what = "net label VSENSE renamed VSENSX on one pin"
        r = subprocess.run([KICAD_CLI, "sch", "erc", "--exit-code-violations", "-o", os.path.join(tmp, "erc.rpt"), sch],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(tmp, "erc.rpt"), encoding="utf-8") as f:
            line = re.search(r"\*\* ERC messages: .*", f.read()).group(0)
        check("ERC rejects: " + what, r.returncode != 0, line.strip("* ") + " (exit %d)" % r.returncode)
        r = subprocess.run([sys.executable, os.path.join(HERE, "check_netlist.py")], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, env=dict(os.environ, KICAD_CLI=KICAD_CLI, SCH_FILE=sch))
        check("check_netlist.py rejects: " + what, r.returncode != 0 and "FAIL" in r.stdout,
              "; ".join(l for l in r.stdout.splitlines() if l.startswith("FAIL"))[:160] + " (exit %d)" % r.returncode)
    print("RESULT: %s (%d/%d)" % ("PASS" if all(results) else "FAIL", sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--sabotage"]:              # child: write sys.argv[4] = sys.argv[3] with a case applied
        b = pcbnew.LoadBoard(sys.argv[3])
        print(CASES[sys.argv[2]](b))
        pcbnew.SaveBoard(sys.argv[4], b)
        sys.exit(0)
    if sys.argv[1:2] == ["--measure"]:               # child: measurements of one board file as JSON
        print(json.dumps(measure(sys.argv[2])))
        sys.exit(0)
    sys.exit(main())
