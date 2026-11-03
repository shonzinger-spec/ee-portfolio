#!/usr/bin/env python3
"""Generate the 2-layer PCB of the Smart Power-Monitoring IoT Node from its KiCad 10 schematic.

Writes, next to this script:
  power_node.kicad_pcb   the board
  power_node.kicad_pro   KiCad adds the board's design rules and net classes to the project file
  power_node.kicad_dru   custom DRC rules: the power traces, and every GND track in the ground-return
                         rule area GND_RETURN, must stay at least 1.9 mm wide

Needs KiCad 10's Python (it imports pcbnew), kicad-cli, Java 25 and the Freerouting 2.5.0 jar
(Maven Central, app.freerouting:freerouting:2.5.0, file freerouting-2.5.0-executable.jar):

  FREEROUTING_JAR=/path/to/freerouting-2.5.0-executable.jar <KiCad python3> gen_pcb.py
  <KiCad python3> gen_pcb.py --export   also write ../fab/ (Gerbers, drill), ../pcb_top.png,
                                        ../pcb_bottom.png and ../../docs/pcb_drc.rpt

Flow: netlist from the schematic (kicad-cli) -> footprints at the coordinates in PLACE -> outline,
mounting holes and the antenna keep-out -> power, ground-return and Kelvin-sense traces drawn
here and locked -> GND pours on both layers -> Freerouting routes the rest (+3V3, SDA, SCL) ->
zone fill, plus GND vias wherever routing cut a patch off one pour -> save.
KiCad's UUID generator is seeded and Freerouting runs single-threaded, so a re-run writes the same
board file byte for byte.
Coordinates are millimetres from the board's top-left corner, y downwards.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "power_node"
SCH = os.path.join(HERE, PROJECT + ".kicad_sch")
PCB = os.path.join(HERE, PROJECT + ".kicad_pcb")
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
KICAD_CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or KICAD_APP + "/MacOS/kicad-cli"
FP_DIR = os.environ.get("KICAD10_FOOTPRINT_DIR") or (
    KICAD_APP + "/SharedSupport/footprints" if sys.platform == "darwin" else "/usr/share/kicad/footprints")
JAVA = os.environ.get("JAVA", "java")
FREEROUTING_JAR = os.environ.get("FREEROUTING_JAR", "freerouting-2.5.0-executable.jar")

UUID_SEED = 20261103            # KIID.SeedGenerator: every UUID in the board file is reproducible
ORIGIN = (100.0, 60.0)          # board top-left corner on the A4 sheet
W, H, CORNER_R = 75.0, 50.0, 2.0

POWER_W = 2.0                   # VSUPPLY, VLOAD and the ground return; see trace_width.py
RETURN_AREA = ("GND_RETURN", (65.0, 10.5, 68.5, 39.5))   # rule area around the J2.2 -> J1.2 return;
                                # any GND track drawn in it is held to the power width by DRC
SUPPLY_W = 0.4                  # +3V3 and GND signal connections
SIG_W = 0.25
NETCLASSES = [                  # name, track, clearance, via diameter, via drill, nets
    ("Power", POWER_W, 0.3, 0.8, 0.4, ["/VSUPPLY", "/VLOAD"]),
    ("Supply", SUPPLY_W, 0.2, 0.8, 0.4, ["+3V3", "GND"]),
]

# ref: (x, y, rotation in degrees) of the footprint origin (pad 1 for U1 and the headers)
PLACE = {
    "U1": (18.0, 1.6, 0),       # DevKitC sockets; pin 1 1.6 mm from the edge the antenna overhangs
    "J3": (7.0, 21.0, 0),       # OLED header, GND VCC SCL SDA from the top
    "C3": (10.0, 22.27, 90),
    "U2": (52.0, 22.0, 0),      # INA219: IN+/IN- face the shunt, SDA/SCL face U1
    "C1": (57.0, 20.795, 270),
    "C2": (57.0, 23.8, 90),
    "R2": (59.6, 20.02, 180),
    "R3": (59.6, 21.57, 180),
    "R1": (64.0, 25.0, 90),     # shunt, current upwards: pad 1 (supply) below, pad 4 (load) above
    "J2": (64.635, 12.0, 90),   # LOAD: pin 1 in line with R1 pad 4
    "J1": (63.365, 38.0, 90),   # 5V IN: pin 1 in line with R1 pad 1
    "R4": (47.0, 27.5, 90),
    "R5": (49.2, 27.5, 90),
}
REFS = {                        # reference text: x, y, size; parts not listed keep the library position
    "C1": (57.0, 18.6, 0.8), "R2": (59.6, 18.6, 0.8), "R3": (60.0, 23.3, 0.8), "C2": (57.0, 26.3, 0.8),
    "R4": (47.0, 30.0, 0.8), "R5": (49.2, 30.0, 0.8), "C3": (12.2, 22.27, 0.8), "R1": (60.8, 29.0, 1.0),
}
HOLES = [(3.5, 3.5), (71.5, 3.5), (3.5, 46.5), (71.5, 46.5)]   # M3, all >= 15 mm from the antenna
STITCH = ([(x, y) for y in (16.0, 24.0, 32.0, 40.0) for x in (24.0, 30.7, 37.4)] +     # under the DevKitC
          [(12.0, 9.0), (12.0, 38.0), (7.0, 42.0), (47.0, 10.0), (55.0, 10.0), (55.0, 33.0), (48.0, 40.0),
           (57.0, 42.0), (69.5, 16.0), (69.5, 24.0), (69.5, 32.0)])                     # GND vias, both pours
KEEPOUT = (20.1, -0.5, 41.3, 8.0)   # x0, y0, x1, y1: between the DevKitC rows, from the edge in
SILK = [                            # text, x, y, size
    ("ANTENNA KEEP-OUT", 30.7, 4.6, 1.0),
    ("OLED", 7.0, 31.2, 1.0),
    ("GND", 3.6, 21.0, 0.8), ("VCC", 3.6, 23.54, 0.8), ("SCL", 3.6, 26.08, 0.8), ("SDA", 3.6, 28.62, 0.8),
    ("LOAD", 65.9, 8.4, 1.0), ("+", 64.635, 10.0, 0.8), ("GND", 67.175, 10.0, 0.8),
    ("+", 63.365, 40.0, 0.8), ("GND", 65.905, 40.0, 0.8), ("5V IN", 64.635, 41.6, 1.0),
    ("SMART POWER NODE 01 REV A", 56.5, 46.2, 0.9),
]


def mm(v):
    return pcbnew.FromMM(v)


def P(x, y):
    return pcbnew.VECTOR2I(mm(ORIGIN[0] + x), mm(ORIGIN[1] + y))


# ----------------------------------------------------------------- netlist from the schematic
def read_netlist():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, PROJECT + ".xml")
        subprocess.run([KICAD_CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, SCH],
                       check=True, stdout=subprocess.DEVNULL)
        root = ET.parse(out).getroot()
    comps = {}
    for c in root.iter("comp"):
        ref = c.get("ref")
        if ref.startswith("#"):
            continue
        sp = c.find("sheetpath")
        comps[ref] = dict(value=c.findtext("value"), footprint=c.findtext("footprint"),
                          datasheet=c.findtext("datasheet") or "", description=c.findtext("description") or "",
                          path=sp.get("tstamps") + c.findtext("tstamps").split()[0],
                          sheetname=sp.get("names"))
    nets = {n.get("name"): [(d.get("ref"), d.get("pin")) for d in n.iter("node")]
            for n in root.iter("net")}
    return comps, nets


# ----------------------------------------------------------------- board set-up
def setup(board):
    ds = board.GetDesignSettings()
    board.SetCopperLayerCount(2)
    ds.SetBoardThickness(mm(1.6))
    ds.m_MinClearance = mm(0.2)
    ds.m_TrackMinWidth = mm(0.2)
    ds.m_ViasMinSize = mm(0.6)
    ds.m_ViasMinAnnularWidth = mm(0.15)
    ds.m_MinThroughDrill = mm(0.3)
    ds.m_HoleClearance = mm(0.25)
    ds.m_HoleToHoleMin = mm(0.25)
    ds.m_CopperEdgeClearance = mm(0.5)
    ds.SetAuxOrigin(P(0, H))          # drill/place origin: bottom-left board corner
    ds.SetGridOrigin(P(0, H))
    ns = ds.m_NetSettings
    default = ns.GetDefaultNetclass()
    default.SetTrackWidth(mm(SIG_W))
    default.SetClearance(mm(0.2))
    default.SetViaDiameter(mm(0.6))
    default.SetViaDrill(mm(0.3))
    for name, width, clearance, via_d, via_drill, nets in NETCLASSES:
        nc = pcbnew.NETCLASS(name)
        nc.SetTrackWidth(mm(width))
        nc.SetClearance(mm(clearance))
        nc.SetViaDiameter(mm(via_d))
        nc.SetViaDrill(mm(via_drill))
        ns.SetNetclass(name, nc)
        for net in nets:
            ns.SetNetclassPatternAssignment(net, name)
    tb = board.GetTitleBlock()
    tb.SetTitle("Smart Power-Monitoring IoT Node")
    tb.SetDate("2026-11-03")
    tb.SetRevision("A")
    tb.SetCompany("Shon Zinger - EE portfolio")
    tb.SetComment(0, "2 layers, 1.6 mm FR4, 1 oz (35 um) copper")
    tb.SetComment(1, "Generated by gen_pcb.py; signals routed with Freerouting 2.5.0")
    board.SetTitleBlock(tb)


def add_nets(board, nets):
    out = {}
    for name in sorted(nets):
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        out[name] = ni
    return out


def load_footprint(lib_id):
    nick, name = lib_id.split(":")
    lib = os.path.join(HERE, nick + ".pretty") if nick == PROJECT else os.path.join(FP_DIR, nick + ".pretty")
    fp = pcbnew.FootprintLoad(lib, name)
    if fp is None:
        raise SystemExit("footprint %s not found in %s" % (lib_id, lib))
    fp.SetFPID(pcbnew.LIB_ID(nick, name))
    return fp


def place(board, comps, nets, N):
    pin_net = {node: name for name, nodes in nets.items() for node in nodes}
    fps = {}
    for ref in sorted(comps):
        c = comps[ref]
        fp = load_footprint(c["footprint"])
        fp.SetReference(ref)
        fp.SetValue(c["value"])
        fp.SetPath(pcbnew.KIID_PATH(c["path"]))
        fp.SetSheetname(c["sheetname"])
        fp.SetSheetfile(PROJECT + ".kicad_sch")
        fp.GetField(pcbnew.FIELD_T_DATASHEET).SetText(c["datasheet"])     # as Update PCB from
        fp.GetField(pcbnew.FIELD_T_DESCRIPTION).SetText(c["description"])  # Schematic would set them
        x, y, rot = PLACE[ref]
        fp.SetPosition(P(x, y))          # before Add(): items moved after Add() leave the zone
        fp.SetOrientationDegrees(rot)    # filler's view of them stale in this session
        if ref in REFS:
            rx, ry, size = REFS[ref]
            field = fp.Reference()
            field.SetPosition(P(rx, ry))
            field.SetTextAngleDegrees(0)
            field.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
            field.SetTextThickness(mm(0.15 * size))
        board.Add(fp)
        for pad in fp.Pads():
            name = pin_net.get((ref, pad.GetNumber()))
            if name:
                pad.SetNet(N[name])
        fps[ref] = fp
    # the 2 A load current returns through J1.2/J2.2: connect them to the pours without thermal spokes
    for ref in ("J1", "J2"):
        fps[ref].FindPadByNumber("2").SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)
    for k, (x, y) in enumerate(HOLES):
        fp = load_footprint("MountingHole:MountingHole_3.2mm_M3")
        fp.SetReference("H%d" % (k + 1))
        fp.Reference().SetVisible(False)     # the library puts it outside the board at the top edge
        fp.SetBoardOnly(True)
        fp.SetPosition(P(x, y))
        board.Add(fp)
    return fps


def edge(board, shape, *pts):
    s = pcbnew.PCB_SHAPE(board, shape)
    if shape == pcbnew.SHAPE_T_ARC:
        s.SetArcGeometry(*[P(*p) for p in pts])
    else:
        s.SetStart(P(*pts[0]))
        s.SetEnd(P(*pts[1]))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(mm(0.1))
    board.Add(s)


def outline(board):
    r, c = CORNER_R, CORNER_R * (1 - 0.5 ** 0.5)
    seg, arc = pcbnew.SHAPE_T_SEGMENT, pcbnew.SHAPE_T_ARC
    edge(board, seg, (r, 0), (W - r, 0))
    edge(board, arc, (W - r, 0), (W - c, c), (W, r))
    edge(board, seg, (W, r), (W, H - r))
    edge(board, arc, (W, H - r), (W - c, H - c), (W - r, H))
    edge(board, seg, (W - r, H), (r, H))
    edge(board, arc, (r, H), (c, H - c), (0, H - r))
    edge(board, seg, (0, H - r), (0, r))
    edge(board, arc, (0, r), (c, c), (r, 0))


def polygon_zone(board, pts):
    z = pcbnew.ZONE(board)
    poly = z.Outline()
    poly.NewOutline()
    for x, y in pts:
        p = P(x, y)
        poly.Append(p.x, p.y)
    return z


def keepout(board):
    x0, y0, x1, y1 = KEEPOUT
    z = polygon_zone(board, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    z.SetIsRuleArea(True)
    z.SetZoneName("ANTENNA_KEEPOUT")
    z.SetLayerSet(pcbnew.LSET.AllCuMask(2))
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(True)
    z.SetDoNotAllowZoneFills(True)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowFootprints(False)
    board.Add(z)


def return_area(board):
    """Rule area that restricts nothing by itself; power_node.kicad_dru holds every GND track in it
    to the power width, so a ground return that is redrawn there, not just one narrowed in place,
    still has to be wide. Added after routing, so Freerouting never sees it."""
    name, (x0, y0, x1, y1) = RETURN_AREA
    z = polygon_zone(board, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    z.SetIsRuleArea(True)
    z.SetZoneName(name)
    z.SetLayerSet(pcbnew.LSET.AllCuMask(2))
    z.SetDoNotAllowTracks(False)
    z.SetDoNotAllowVias(False)
    z.SetDoNotAllowZoneFills(False)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowFootprints(False)
    board.Add(z)


def pours(board, gnd):
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = polygon_zone(board, [(0, 0), (W, 0), (W, H), (0, H)])
        z.SetLayer(layer)
        z.SetNet(gnd)
        z.SetZoneName("GND_" + pcbnew.BOARD.GetStandardLayerName(layer).split(".")[0])
        z.SetLocalClearance(mm(0.3))
        z.SetMinThickness(mm(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        z.SetThermalReliefGap(mm(0.5))
        z.SetThermalReliefSpokeWidth(mm(0.5))
        z.SetAssignedPriority(0)
        board.Add(z)


def silk(board):
    for text, x, y, size in SILK:
        t = pcbnew.PCB_TEXT(board)
        t.SetText(text)
        t.SetPosition(P(x, y))
        t.SetLayer(pcbnew.F_SilkS)
        t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        t.SetTextThickness(mm(0.15 * size))
        board.Add(t)


# ----------------------------------------------------------------- hand-routed copper
def track(board, net, layer, width, pts):
    out = []
    for a, b in zip(pts, pts[1:]):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(P(*a))
        t.SetEnd(P(*b))
        t.SetWidth(mm(width))
        t.SetLayer(layer)
        t.SetNet(net)
        t.SetLocked(True)
        board.Add(t)
        out.append(t)
    return out


def copper(board, locked):
    """Tracks and vias, locked or not. Indexed access: BOARD.GetTracks() iterates with a SWIG
    iterator that fails under Python 3.14."""
    items = board.Tracks()
    return [items[i] for i in range(len(items)) if items[i].IsLocked() == locked]


def via(board, net, at, diameter=0.6, drill=0.3):
    v = pcbnew.PCB_VIA(board)
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetPosition(P(*at))
    v.SetWidth(mm(diameter))
    v.SetDrill(mm(drill))
    v.SetNet(net)
    v.SetLocked(True)
    board.Add(v)


def route(board, net, pad):
    """The power path, its ground return, the Kelvin sense lines, U2's input filter, decoupling and
    address straps. Everything here is locked, so Freerouting routes around it."""
    F, B = pcbnew.F_Cu, pcbnew.B_Cu
    track(board, net("/VSUPPLY"), F, POWER_W, [pad("J1", "1"), pad("R1", "1")])
    track(board, net("/VLOAD"), F, POWER_W, [pad("R1", "4"), pad("J2", "1")])
    j22, j12 = pad("J2", "2"), pad("J1", "2")          # load current return, beside the power path
    dx = j22[0] - j12[0]
    yk = j12[1] - dx - 1.2
    track(board, net("GND"), F, POWER_W, [j22, (j22[0], yk), (j12[0], yk + dx), j12])
    # Kelvin sense: SENSE_N leaves R1 pad 3 on U2's side. Pad 2 (SENSE_P) lies on the far side of the
    # current path, so SENSE_P drops to B.Cu at the pad and comes back up beside R2.
    track(board, net("/SENSE_N"), F, SIG_W, [pad("R1", "3"), pad("R3", "1")])
    r12, r21 = pad("R1", "2"), pad("R2", "1")
    v1, v2 = (r12[0], r12[1] + 1.57), (r21[0] + 1.175, r21[1])
    track(board, net("/SENSE_P"), F, SIG_W, [r12, v1])
    via(board, net("/SENSE_P"), v1)
    track(board, net("/SENSE_P"), B, SIG_W, [v1, (v2[0], v1[1] - (v1[0] - v2[0])), v2])
    via(board, net("/SENSE_P"), v2)
    track(board, net("/SENSE_P"), F, SIG_W, [v2, r21])
    # 10 ohm + 10 ohm + 100 nF filter into IN+ (pin 8) and IN- (pin 7)
    track(board, net("/INA_INP"), F, SIG_W, [pad("R2", "2"), pad("C1", "1"), pad("U2", "8")])
    track(board, net("/INA_INN"), F, SIG_W, [pad("R3", "2"), pad("C1", "2"), pad("U2", "7")])
    # C2 across VS (pin 5) and GND (pin 6), with a via to the B.Cu pour
    c22 = pad("C2", "2")
    vg = (c22[0] + 1.5, c22[1])
    track(board, net("GND"), F, SUPPLY_W, [pad("U2", "6"), c22, vg])
    via(board, net("GND"), vg)
    track(board, net("+3V3"), F, SUPPLY_W, [pad("U2", "5"), pad("C2", "1")])
    # A1 (pin 1) and A0 (pin 2) to GND: address 0x40
    u21, u22 = pad("U2", "1"), pad("U2", "2")
    xs, ym = u21[0] - 0.675, (u21[1] + u22[1]) / 2
    track(board, net("GND"), F, 0.3, [u21, (xs, u21[1]), (xs, u22[1]), u22])
    track(board, net("GND"), F, 0.3, [(xs, ym), (xs - 1.35, ym)])
    via(board, net("GND"), (xs - 1.35, ym))
    # SDA (pin 3) and SCL (pin 4) out to the pull-ups R4 and R5, whose +3V3 ends are joined
    u23, r42, r52 = pad("U2", "3"), pad("R4", "2"), pad("R5", "2")
    track(board, net("/SDA"), F, SIG_W, [u23, (r42[0], u23[1]), r42])
    track(board, net("/SCL"), F, SIG_W, [(r52[0], pad("U2", "4")[1]), r52])
    track(board, net("+3V3"), F, SUPPLY_W, [pad("R4", "1"), pad("R5", "1")])
    # C3 at the OLED header
    track(board, net("GND"), F, SUPPLY_W, [pad("C3", "2"), pad("J3", "1")])
    track(board, net("+3V3"), F, SUPPLY_W, [pad("C3", "1"), pad("J3", "2")])
    # ground stitching: along the keep-out's inner edge under the module, and across the board
    for k in range(9):
        via(board, net("GND"), (21.0 + 2.5 * k, KEEPOUT[3] + 0.9))
    for at in STITCH:
        via(board, net("GND"), at)


def stitch_voids(board):
    """GND vias where one pour has copper and the other has a gap that routing cut off: the via ties
    the cut-off patch to the other layer, so the next fill keeps it instead of dropping it as an
    island. Candidates on a 1 mm grid, clear of every pad, track, via, the keep-out and the edge."""
    pours = {z.GetLayer(): z for z in board.Zones() if not z.GetIsRuleArea()}
    rule_areas = [z for z in board.Zones() if z.GetIsRuleArea()]
    footprints = board.Footprints()
    pads = [p for i in range(len(footprints)) for p in footprints[i].Pads()]
    items = board.Tracks()
    items = [items[i] for i in range(len(items))]
    vias = [t.GetPosition() for t in items if t.GetClass() == "PCB_VIA"]
    clear = mm(0.3 + 0.35)               # via radius + 0.3 mm clearance + margin
    added = []
    for iy in range(2, int(H) - 1):
        for ix in range(2, int(W) - 1):
            p = P(ix, iy)
            on_f = pours[pcbnew.F_Cu].GetFilledPolysList(pcbnew.F_Cu).Contains(p)
            on_b = pours[pcbnew.B_Cu].GetFilledPolysList(pcbnew.B_Cu).Contains(p)
            if on_f == on_b:
                continue
            if any(z.Outline().Collide(p, mm(1.0)) for z in rule_areas):
                continue
            if any(q.HitTest(p, clear) for q in pads) or any(t.HitTest(p, clear) for t in items):
                continue
            if any((p.x - q.x) ** 2 + (p.y - q.y) ** 2 < mm(2.5) ** 2 for q in vias + added):
                continue
            added.append(p)
    gnd = board.FindNet("GND")
    for p in added:
        v = pcbnew.PCB_VIA(board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(p)
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetNet(gnd)
        v.SetLocked(True)
        board.Add(v)
    return len(added)


# ----------------------------------------------------------------- Freerouting
def freeroute(board):
    with tempfile.TemporaryDirectory() as tmp:
        dsn, ses = os.path.join(tmp, PROJECT + ".dsn"), os.path.join(tmp, PROJECT + ".ses")
        if not pcbnew.ExportSpecctraDSN(board, dsn):
            raise SystemExit("Specctra DSN export failed")
        cmd = [JAVA, "-Djava.awt.headless=true", "-jar", FREEROUTING_JAR, "-de", dsn, "-do", ses,
               "--gui.enabled=false", "--router.autorouter.max_passes=20",
               "--router.autorouter.max_threads=1", "--router.optimizer.max_threads=1",
               "--router.automatic_neckdown=false"]       # keep every trace at its class width
        r = subprocess.run(cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in r.stdout.splitlines():
            if re.search(r"(auto-?rout|optimi|incomplete|unrouted|completed|error)", line, re.I):
                print("freerouting:", line.split("  ", 1)[-1].strip())
        if r.returncode or not os.path.exists(ses):
            raise SystemExit("Freerouting failed (exit %d)" % r.returncode)
        if not pcbnew.ImportSpecctraSES(board, ses):
            raise SystemExit("Specctra SES import failed")


# ----------------------------------------------------------------- output
DRU = """(version 1)
(rule "Power traces stay at least 1.9 mm wide (2.0 mm drawn, sized by trace_width.py)"
	(condition "A.NetClass == 'Power'")
	(constraint track_width (min 1.9mm)))
(rule "The ground return (GND tracks in rule area GND_RETURN) stays at least 1.9 mm wide"
	(condition "A.NetName == 'GND' && A.Type == 'Track' && A.intersectsArea('GND_RETURN')")
	(constraint track_width (min 1.9mm)))
"""


def save(board):
    pcbnew.SaveBoard(PCB, board)
    with open(os.path.join(HERE, PROJECT + ".kicad_dru"), "w", encoding="utf-8", newline="\n") as f:
        f.write(DRU)


def run(cmd):
    """Run from this directory with project paths relative to it, so the commands and kicad-cli's
    own messages print short paths."""
    top = os.path.dirname(os.path.dirname(HERE))
    cmd = [os.path.relpath(c, HERE) + (os.sep if c.endswith(os.sep) else "") if c.startswith(top) else c
           for c in cmd]
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=HERE, check=True)


def export():
    hw = os.path.normpath(os.path.join(HERE, ".."))
    fab = os.path.join(hw, "fab")
    docs = os.path.normpath(os.path.join(hw, "..", "docs"))
    # DRC first: a board with any violation, unconnected pad or parity error stops here,
    # before the fabrication files are touched
    run([KICAD_CLI, "pcb", "drc", "--schematic-parity", "--severity-all", "--exit-code-violations",
         "-o", os.path.join(docs, "pcb_drc.rpt"), PCB])
    os.makedirs(fab, exist_ok=True)
    for f in os.listdir(fab):                      # previous outputs of this function only
        if f.startswith(PROJECT):
            os.remove(os.path.join(fab, f))
    layers = "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
    run([KICAD_CLI, "pcb", "export", "gerbers", "--layers", layers, "--subtract-soldermask",
         "--use-drill-file-origin", "-o", fab + os.sep, PCB])
    run([KICAD_CLI, "pcb", "export", "drill", "--format", "excellon", "--drill-origin", "plot",
         "--excellon-units", "mm", "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2",
         "-o", fab + os.sep, PCB])
    for side in ("top", "bottom"):
        run([KICAD_CLI, "pcb", "render", "--side", side, "--width", "1600", "--height", "1100", "--zoom", "1.15",
             "--quality", "basic", "--background", "opaque", "-o", os.path.join(hw, "pcb_%s.png" % side), PCB])


def main():
    sys.stdout.reconfigure(line_buffering=True)    # keep this script's lines in order with kicad-cli's
    comps, nets = read_netlist()
    missing = sorted(set(comps) - set(PLACE))
    if missing:
        raise SystemExit("no placement for %s" % ", ".join(missing))
    board = pcbnew.NewBoard(PCB)
    # seeded after NewBoard and after every reload: loading the project file draws UUIDs too, more
    # or fewer depending on what the file holds, which would shift the sequence between runs
    pcbnew.KIID.SeedGenerator(UUID_SEED)
    setup(board)
    N = add_nets(board, nets)
    fps = place(board, comps, nets, N)
    board.SynchronizeNetsAndNetClasses(True)
    outline(board)
    keepout(board)
    silk(board)

    def pad(ref, number):
        p = fps[ref].FindPadByNumber(number).GetPosition()
        return pcbnew.ToMM(p.x) - ORIGIN[0], pcbnew.ToMM(p.y) - ORIGIN[1]

    route(board, lambda name: N[name], pad)
    pours(board, N["GND"])
    locked = len(copper(board, True))
    freeroute(board)
    print("tracks and vias: %d drawn here (locked), %d from Freerouting"
          % (len(copper(board, True)), len(copper(board, False))))
    if len(copper(board, True)) != locked:
        raise SystemExit("the SES import changed the locked copper")
    return_area(board)
    pcbnew.SaveBoard(PCB, board)
    stitched = 0
    while True:                          # fill on the board as saved, not on this session's caches
        board = pcbnew.LoadBoard(PCB)
        pcbnew.KIID.SeedGenerator(UUID_SEED + 1 + stitched)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        added = stitch_voids(board)
        pcbnew.SaveBoard(PCB, board)
        stitched += added
        if not added:
            break
    print("GND vias added in pour gaps:", stitched)
    board.BuildConnectivity()
    print("unrouted connections:", board.GetConnectivity().GetUnconnectedCount(False))
    save(board)
    print("wrote", os.path.relpath(PCB, os.path.join(HERE, "..", "..")))
    if "--export" in sys.argv[1:]:
        export()


if __name__ == "__main__":
    main()
