#!/usr/bin/env python3
"""Generate the 2-layer PCB of the 12 V -> 5 V / 3 A synchronous buck from its KiCad 10 schematic.

Writes, next to this script:
  sync_buck.kicad_pcb   the board
  sync_buck.kicad_pro   KiCad adds the board's design rules and net classes to the project file
  sync_buck.kicad_dru   custom DRC rules: the power copper (VIN, SW, VOUT, VLOAD and the hot-loop
                        ground) must stay at least POWER_MIN wide (sized by trace_width.py)

Needs KiCad 10's Python (it imports pcbnew) and kicad-cli. No autorouter: every track and via is
drawn here, so a re-run writes the same board file byte for byte.

  <KiCad python3> gen_pcb.py            write the board
  <KiCad python3> gen_pcb.py --export   also write ../fab/ (Gerbers, drill), ../pcb_top.png,
                                        ../pcb_bottom.png and ../../docs/pcb_drc.rpt

Flow: netlist from the schematic (kicad-cli) -> footprints at PLACE -> outline and M3 holes ->
every connection drawn by route() -> rule areas for the power-width rules -> GND pours on both
layers -> zone fill, plus GND vias wherever a pour patch is reachable only on one layer -> save.
Coordinates are millimetres from the board's top-left corner, y downwards.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = "sync_buck"
SCH = os.path.join(HERE, PROJECT + ".kicad_sch")
PCB = os.path.join(HERE, PROJECT + ".kicad_pcb")
KICAD_APP = "/Applications/KiCad/KiCad.app/Contents"
KICAD_CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or KICAD_APP + "/MacOS/kicad-cli"
FP_DIR = os.environ.get("KICAD10_FOOTPRINT_DIR") or (
    KICAD_APP + "/SharedSupport/footprints" if sys.platform == "darwin" else "/usr/share/kicad/footprints")

UUID_SEED = 20261104
ORIGIN = (100.0, 60.0)          # board top-left corner on the A4 sheet
W, H, CORNER_R = 54.0, 40.0, 2.0

POWER_W = 2.0                   # drawn width of the power copper; trace_width.py: 1.37 mm needed at 3 A
POWER_MIN = 1.4                 # DRC floor for power copper (>= IPC-2221 minimum at 3 A, 10 degC rise)
GATE_W = 0.4                    # HO/GH/LO/GL/HB/HS and the driver supply
SIG_W = 0.25
NETCLASSES = [                  # name, track, clearance, via diameter, via drill, nets
    ("Power", POWER_W, 0.25, 0.8, 0.4, ["/VIN", "/SW", "/VOUT", "/VLOAD"]),
    ("Gate", GATE_W, 0.2, 0.6, 0.3, ["/HO", "/GH", "/LO", "/GL", "/HB", "/SNB"]),
]

O = (17.0, 12.5)                # power-stage origin: Q1's footprint origin


def rel(x, y):
    return (O[0] + x, O[1] + y)


# ref: (x, y, rotation in degrees) of the footprint origin
PLACE = {
    # input
    "J1": (2.4, 13.1, 0),                   # VIN 12V: pin 1 VIN, pin 2 GND
    "C3": (8.6, 13.1, 90),                  # 100 uF bulk, + pad on the VIN line
    # hot loop: C1/C2 -> Q1 (tab = drain) -> Q1 source lead -> Q2 tab (drain) -> Q2 source lead -> C1/C2
    "Q1": rel(0.0, 0.0) + (90,),            # tab north (VIN), leads south: G, D, S from the west
    "Q2": rel(8.6, 4.0) + (180,),           # tab west (SW) against Q1's source lead, leads east: S, D, G
    "C1": rel(5.95, -1.45) + (0,),          # pad 1 VIN at Q1's tab, pad 2 GND toward Q2's source
    "C2": rel(5.95, -4.85) + (0,),
    "R5": rel(15.9, 3.1) + (90,),           # snubber across Q2 drain (centre lead) and source
    "C6": rel(15.9, 0.0) + (90,),
    # output: L1 straight below the switch node, then C7/C8, R6 and J2 along the bottom
    "L1": rel(3.0, 14.95) + (-90,),         # pad 1 (SW) north, pad 2 (VOUT) south
    "C7": (31.4, 34.0, 0),
    "C8": (37.3, 32.55, -90),
    "R6": (42.75, 31.235, 0),               # Kelvin shunt: force 1 -> 4, sense pads 2 (SW) and 3 (NE)
    "J2": (43.54, 37.6, -90),               # VOUT 5V: pin 1 VLOAD (east), pin 2 GND (west)
    # gate driver east of L1, beside Q2's gate
    "U1": (32.2, 24.1, 0),
    "C4": (27.5, 21.6, -90),                # VDD decoupling beside pin 1
    "R1": (27.5, 27.8, -90),                # gate resistor
    "R3": (30.6, 27.65, 180),               # gate-source pull-down, below pin 4
    "C5": (27.5, 24.735, -90),              # bootstrap cap straddling HB (pin 2) and HS (pin 4)
    "R2": (34.5, 18.6, 90),
    "R4": (36.1, 18.6, -90),
    "R10": (38.6, 25.9, -90),               # PWM pull-downs, between U1 and J3
    "R11": (40.2, 22.6, -90),
    # ESP32 header on the east edge, analog parts beside it, away from the switch node and L1
    "J3": (51.0, 9.6, 0),                   # +3V3, GND, PWM_H, PWM_L, VSENSE, ISENSE, VINSENSE
    "U2": (43.9, 27.2, 0),                  # INA181A3: IN+/IN- face the shunt
    "C9": (47.6, 25.9, 0),
    "R9": (42.763, 23.9, 90),
    "C11": (44.6, 23.075, -90),
    "R7": (48.2, 31.6, 90),                 # VLOAD tap at R6 pad 4
    "R8": (46.4, 20.6, -90),                # R8 and C10 at the ADC end of VSENSE
    "C10": (48.4, 20.6, -90),
    "R12": (51.4, 27.5, 180),               # VINSENSE divider below J3 pin 7
    "R13": (51.4, 29.2, 0),
    "C12": (51.4, 30.9, 0),
}
HOLES = [(3.5, 3.5), (W - 3.5, 3.5), (3.5, H - 3.5), (W - 3.5, H - 3.5)]


def mm(v):
    return pcbnew.FromMM(v)


def P(x, y):
    return pcbnew.VECTOR2I(mm(ORIGIN[0] + x), mm(ORIGIN[1] + y))


# ----------------------------------------------------------------- netlist from the schematic
def read_netlist():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, PROJECT + ".xml")
        subprocess.run([KICAD_CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, SCH],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    ds.SetAuxOrigin(P(0, H))
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
    tb.SetTitle("02 - Sync buck 12 V to 5 V / 3 A")
    tb.SetDate("2026-11-03")
    tb.SetRevision("A")
    tb.SetCompany("Shon Zinger - EE portfolio")
    tb.SetComment(0, "2 layers, 1.6 mm FR4, 1 oz (35 um) copper")
    tb.SetComment(1, "Generated by gen_pcb.py; every track drawn by the script (no autorouter)")
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
    fp = pcbnew.FootprintLoad(os.path.join(FP_DIR, nick + ".pretty"), name)
    if fp is None:
        raise SystemExit("footprint %s not found" % lib_id)
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
        fp.GetField(pcbnew.FIELD_T_DATASHEET).SetText(c["datasheet"])
        fp.GetField(pcbnew.FIELD_T_DESCRIPTION).SetText(c["description"])
        x, y, rot = PLACE[ref]
        fp.SetPosition(P(x, y))
        fp.SetOrientationDegrees(rot)
        board.Add(fp)
        for pad in fp.Pads():
            name = pin_net.get((ref, pad.GetNumber()))
            if name:
                pad.SetNet(N[name])
        fps[ref] = fp
    for k, (x, y) in enumerate(HOLES):
        fp = load_footprint("MountingHole:MountingHole_3.2mm_M3")
        fp.SetReference("H%d" % (k + 1))
        fp.Reference().SetVisible(False)
        fp.SetBoardOnly(True)
        fp.SetPosition(P(x, y))
        board.Add(fp)
    return fps


def place_refs(board, fps):
    """Reference text on F.Silkscreen at 0.8 mm, at the first spot round each part where it clears
    every pad, every footprint's silkscreen, the other references, the holes and the board edge.
    A part with no such spot keeps its reference on F.Fab only (listed in the generator log)."""
    size = 0.8
    pads = []
    silk = []
    for ref, fp in fps.items():
        pads += [q.GetBoundingBox() for q in fp.Pads()]
        silk += [g.GetBoundingBox() for g in fp.GraphicalItems() if g.GetLayer() == pcbnew.F_SilkS]
    holes = [pcbnew.BOX2I(P(x - 3.0, y - 3.0), pcbnew.VECTOR2I(mm(6.0), mm(6.0))) for x, y in HOLES]
    placed, hidden = [], []
    edge_box = pcbnew.BOX2I(P(0.4, 0.4), pcbnew.VECTOR2I(mm(W - 0.8), mm(H - 0.8)))

    def grow(b, d):
        b = pcbnew.BOX2I(b.GetPosition(), b.GetSize())
        b.Inflate(mm(d))
        return b

    for ref in sorted(fps, key=lambda r: (len(fps[r].Pads()), r)):
        fp = fps[ref]
        field = fp.Reference()
        field.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        field.SetTextThickness(mm(0.13))
        field.SetTextAngleDegrees(0)
        field.SetKeepUpright(True)
        c = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
        cx, cy = pcbnew.ToMM(c.GetCenter().x) - ORIGIN[0], pcbnew.ToMM(c.GetCenter().y) - ORIGIN[1]
        hw, hh = pcbnew.ToMM(c.GetWidth()) / 2, pcbnew.ToMM(c.GetHeight()) / 2
        cands = [(cx, cy - hh - 0.45), (cx, cy + hh + 0.45), (cx - hw - 0.2 - 0.25 * len(ref), cy),
                 (cx + hw + 0.2 + 0.25 * len(ref), cy), (cx, cy)]
        for dx in (-0.8, 0.8, -1.6, 1.6, -2.4, 2.4, -3.2, 3.2):
            cands += [(cx + dx, cy - hh - 0.45), (cx + dx, cy + hh + 0.45)]
        cands += [(cx + fx * hw, cy + fy * hh) for fy in (0, -0.3, 0.3, -0.6, 0.6) for fx in (0, -0.4, 0.4)]
        for x, y in cands:
            field.SetPosition(P(x, y))
            bb = field.GetBoundingBox()
            if not edge_box.Contains(bb):
                continue
            if any(bb.Intersects(grow(q, 0.2)) for q in pads):
                continue
            if any(bb.Intersects(grow(g, 0.15)) for g in silk):
                continue
            if any(bb.Intersects(grow(o, 0.15)) for o in placed) or any(bb.Intersects(h) for h in holes):
                continue
            placed.append(bb)
            break
        else:
            field.SetLayer(pcbnew.F_Fab)
            field.SetPosition(P(cx, cy))
            hidden.append(ref)
    print("reference text on F.Fab only (no clear silkscreen spot):", ", ".join(hidden) or "none")


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


# ----------------------------------------------------------------- copper drawn by this script
F, B = pcbnew.F_Cu, pcbnew.B_Cu
PV = (0.8, 0.4)                 # power vias (hot-loop ground, output ground)


class Copper:
    def __init__(self, board, N, fps):
        self.board, self.N, self.fps = board, N, fps

    def pad(self, ref, number, big=False):
        """Pad centre in board mm. big=True picks the larger of two same-numbered pads (a DPAK tab)."""
        pads = [p for p in self.fps[ref].Pads() if p.GetNumber() == number]
        p = max(pads, key=lambda q: q.GetSize(F).x * q.GetSize(F).y) if big else min(
            pads, key=lambda q: q.GetSize(F).x * q.GetSize(F).y)
        v = p.GetPosition()
        return round(pcbnew.ToMM(v.x) - ORIGIN[0], 4), round(pcbnew.ToMM(v.y) - ORIGIN[1], 4)

    def track(self, net, layer, width, pts):
        for a, b in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(P(*a))
            t.SetEnd(P(*b))
            t.SetWidth(mm(width))
            t.SetLayer(layer)
            t.SetNet(self.N[net])
            t.SetLocked(True)
            self.board.Add(t)

    def via(self, net, at, size=(0.6, 0.3)):
        v = pcbnew.PCB_VIA(self.board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(P(*at))
        v.SetWidth(mm(size[0]))
        v.SetDrill(mm(size[1]))
        v.SetNet(self.N[net])
        v.SetLocked(True)
        self.board.Add(v)

    def hop(self, net, width, top_a, pts, top_b):
        """A short bottom-layer jump: via at pts[0], B.Cu along pts, via at pts[-1]; top stubs to the pads."""
        if top_a:
            self.track(net, F, width, [top_a, pts[0]])
        self.via(net, pts[0])
        self.track(net, B, width, pts)
        self.via(net, pts[-1])
        if top_b:
            self.track(net, F, width, [pts[-1], top_b])

    def gnd(self, pad_xy, at, width=0.4, size=(0.6, 0.3)):
        """Ground via beside an SMD ground pad, joined by a short top track."""
        self.track("GND", F, width, [pad_xy, at])
        self.via("GND", at, size)


def route(c):
    p, T, V = c.pad, c.track, c.via
    PW = POWER_W
    # ---- input: J1 -> C3 -> Q1 tab (drain) -> C1/C2
    T("/VIN", F, PW, [p("J1", "1"), p("C3", "1"), (15.2, 13.1)])
    T("/VIN", F, PW, [(19.0, 11.05), p("C1", "1"), p("C2", "1")])
    T("/VIN", F, 1.4, [p("Q1", "2"), (17.0, 14.0)])             # Q1 centre lead to its own tab
    # ---- hot-loop ground: C2 -> C1 -> Q2 source, vias to the B.Cu pour at each ground pad
    T("GND", F, PW, [p("C2", "2"), p("C1", "2"), (27.2, 11.05), (29.9, 13.1), p("Q2", "3")])
    T("GND", F, PW, [p("C2", "2"), (25.9, 7.65)])
    for at in [(25.9, 7.0), (25.9, 8.3), (25.9, 10.45), (25.9, 11.65), (28.6, 11.4), (29.4, 14.9)]:
        V("GND", at, PV)
    # ---- switch node: Q1 source lead -> Q2 tab, and straight down into L1 pad 1
    T("/SW", F, PW, [p("Q1", "3"), (22.0, 17.54)])
    T("/SW", F, 2.4, [(20.6, 17.54), (20.6, 21.9)])
    T("/SW", F, 1.4, [p("Q2", "2"), (27.0, 16.5)])              # Q2 centre lead to its own tab
    # snubber R5 + C6 across Q2 drain (centre lead) and source (hot-loop ground track)
    T("/SW", F, 0.5, [p("Q2", "2"), p("R5", "1")])
    T("/SNB", F, GATE_W, [p("R5", "2"), p("C6", "1")])
    T("GND", F, 1.5, [p("C6", "2"), (31.2, 11.725), (30.9, 13.62), p("Q2", "3")])   # onto Q2's source
    # ---- output: L1 pad 2 -> bus -> C7, C8 -> R6 force pad 1; R6 force pad 4 -> J2
    T("/VOUT", F, PW, [p("L1", "2"), (21.6, 30.6), p("R6", "1")])
    T("/VOUT", F, PW, [(28.7, 30.6), p("C7", "1")])
    T("/VOUT", F, 1.45, [(37.3, 30.6), p("C8", "1")])
    T("/VLOAD", F, PW, [p("R6", "4"), (45.735, 35.6), p("J2", "1")])
    T("GND", F, PW, [p("C7", "2"), (35.3, 34.0), (37.3, 34.3), (38.6, 35.2), p("J2", "2")])
    for at in [(35.6, 33.0), (35.6, 35.0), (38.4, 33.9), (39.9, 36.3)]:
        V("GND", at, PV)
    # ---- gate driver supply: VIN from the top VIN track, 0.4 mm on B.Cu down x = 33.4 and west under U1
    V("/VIN", (33.4, 6.9))
    T("/VIN", B, GATE_W, [(33.4, 6.9), (33.4, 21.5), (28.6, 21.5)])
    V("/VIN", (28.6, 21.5))
    T("/VIN", F, GATE_W, [p("C4", "1"), (28.6, 20.825), (28.6, 22.195), p("U1", "1")])
    c.gnd(p("C4", "2"), (26.4, 22.375))
    # ---- high side. C5 straddles HB/HS at the pins; HO hops under it on B.Cu to R1. The gate loop is two
    # tracks side by side on B.Cu (the lane, 1.0 mm apart): HS leaves from the SW copper beside Q1's source
    # lead (via inside the Q1.3 -> L1 switch-node track), GH from Q1's gate.
    T("/HB", F, GATE_W, [p("U1", "2"), (28.2, 23.465), p("C5", "1")])
    T("/SW", F, 0.5, [p("U1", "4"), (27.5, 26.005), (26.55, 26.005)])                      # HS: pin 4, C5, lane
    T("/SW", F, 0.5, [p("U1", "4"), p("R3", "2")])
    T("/SW", F, 0.5, [p("C5", "2"), (27.5, 26.005)])
    V("/SW", (26.55, 26.005))
    T("/SW", B, 0.5, [(26.55, 26.005), (19.7, 19.1)])                                     # HS lane
    V("/SW", (19.7, 19.1))
    T("/HO", F, GATE_W, [p("U1", "3"), (28.45, 24.735)])
    V("/HO", (28.45, 24.735))
    T("/HO", B, GATE_W, [(28.45, 24.735), (28.45, 26.9)])
    V("/HO", (28.45, 26.9))
    T("/HO", F, GATE_W, [(28.45, 26.9), p("R1", "1")])
    T("/GH", F, GATE_W, [p("R1", "2"), (26.4, 28.625)])
    V("/GH", (26.4, 28.625))
    T("/GH", B, GATE_W, [(26.4, 28.625), (25.84, 26.71), (18.99, 19.81), (14.72, 19.1)])   # GH lane
    V("/GH", (14.72, 19.1))
    T("/GH", F, GATE_W, [(14.72, 19.1), p("Q1", "1")])
    T("/GH", F, GATE_W, [p("R1", "2"), (28.4, 28.9), (31.425, 28.9), p("R3", "1")])                        # pull-down to the gate
    # low side: LO -> R2 -> GL -> Q2 gate, R4 pull-down; return through VSS and the B.Cu pour
    T("/LO", F, GATE_W, [p("U1", "8"), p("R2", "1")])
    T("/GL", F, GATE_W, [p("R4", "1"), p("R2", "2"), (32.4, 17.775), (31.2, 18.78), p("Q2", "1")])
    c.gnd(p("R4", "2"), (37.2, 19.425))
    c.gnd(p("U1", "7"), (36.3, 23.465))
    # ---- PWM from J3 to U1, with the ESP32-reset pull-downs R10/R11; PWM_H hops under PWM_L
    T("/PWM_L", F, SIG_W, [p("U1", "6"), (37.2, 24.735), (38.96, 21.775), p("R11", "1"), (40.2, 18.5),
                           (41.48, 17.22), p("J3", "4")])
    T("/PWM_H", F, SIG_W, [p("U1", "5"), (37.6, 26.005), p("R10", "1")])
    c.hop("/PWM_H", SIG_W, p("R10", "1"), [(38.6, 24.1), (38.0, 23.0), (38.0, 19.6)], None)
    T("/PWM_H", F, SIG_W, [(38.0, 19.6), (38.0, 15.6), (38.92, 14.68), p("J3", "3")])
    c.gnd(p("R10", "2"), (38.6, 27.6), SIG_W)
    c.gnd(p("R11", "2"), (40.2, 24.3), SIG_W)
    # ---- +3V3: J3 pin 1 down the east edge, in between pins 6 and 7, to C9 and U2 VS
    T("+3V3", F, SIG_W, [p("J3", "1"), (52.4, 9.6), (52.4, 23.57), (48.2, 23.57), (46.825, 24.95),
                         p("C9", "1"), p("U2", "6")])
    c.gnd(p("C9", "2"), (49.4, 26.4), SIG_W)
    # ---- VIN for the VINSENSE divider: from C2 along the top edge and down the east edge to R12
    T("/VIN", F, SIG_W, [(21.475, 6.3), (21.475, 5.2), (27.0, 5.2), (28.7, 6.9), (53.0, 6.9), (53.0, 27.5),
                         p("R12", "1")])
    T("/VINSENSE", F, SIG_W, [p("J3", "7"), (50.575, 25.7), p("R12", "2"), p("R13", "1"), p("C12", "1")])
    c.gnd(p("R13", "2"), (53.15, 29.2), SIG_W)
    c.gnd(p("C12", "2"), (53.15, 30.9), SIG_W)
    # ---- Kelvin sense: pad 3 (SH_N) straight to IN-, pad 2 (SH_P) under the shunt on B.Cu to IN+
    T("/SH_N", F, SIG_W, [p("R6", "3"), (45.038, 29.0), p("U2", "4")])
    c.hop("/SH_P", SIG_W, p("R6", "2"), [(39.32, 33.45), (40.5, 33.45), (42.3, 31.65), (42.3, 29.0)], p("U2", "3"))
    c.gnd(p("U2", "2"), (41.5, 27.2), SIG_W)
    c.gnd(p("U2", "5"), (46.25, 27.2), SIG_W)
    # ---- ISENSE: U2 OUT -> R9 -> C11 -> J3 pin 6
    T("/IAMP", F, SIG_W, [p("U2", "1"), p("R9", "1")])
    T("/ISENSE", F, SIG_W, [p("R9", "2"), (43.538, 22.3), p("C11", "1"), p("J3", "6")])
    c.gnd(p("C11", "2"), (44.6, 24.75), SIG_W)
    # ---- VSENSE: R7 taps VLOAD at R6 pad 4, B.Cu north to R8/C10 at J3 pin 5
    T("/VLOAD", F, SIG_W, [(46.88, 32.425), p("R7", "1")])
    c.hop("/VSENSE", SIG_W, p("R7", "2"), [(48.2, 29.9), (47.3, 29.0), (47.3, 25.0), (45.0, 22.7), (45.0, 19.76)],
          None)
    T("/VSENSE", F, SIG_W, [(45.0, 19.76), p("R8", "1"), p("C10", "1"), p("J3", "5")])
    T("GND", F, SIG_W, [p("R8", "2"), p("C10", "2")])
    V("GND", (47.4, 21.375))



# ----------------------------------------------------------------- rule areas, pours, stitching
# Named rule areas that restrict nothing themselves; sync_buck.kicad_dru holds every track of the
# named net inside them to POWER_MIN, so a power track redrawn there, not only one narrowed in place,
# has to be wide. The thin taps of the same nets (driver supply, snubber, HS, dividers) stay outside.
RULE_AREAS = [
    ("PWR_VIN", "/VIN", [(0.6, 11.5), (13.5, 11.5), (13.5, 6.6), (22.6, 6.6), (22.6, 14.8), (0.6, 14.8)]),
    ("PWR_SW", "/SW", [(18.3, 15.2), (28.0, 15.2), (28.0, 20.9), (18.3, 20.9)]),
    ("PWR_VLOAD", "/VLOAD", [(43.0, 30.6), (46.7, 30.6), (46.7, 38.8), (43.0, 38.8)]),
    ("GND_HOT", "GND", [(23.4, 6.0), (32.2, 6.0), (32.2, 14.0), (23.4, 14.0)]),
]
STITCH_PITCH = 3.0              # GND stitching vias on this grid wherever both pours have copper


def polygon_zone(board, pts):
    z = pcbnew.ZONE(board)
    poly = z.Outline()
    poly.NewOutline()
    for x, y in pts:
        q = P(x, y)
        poly.Append(q.x, q.y)
    return z


def rule_areas(board):
    for name, _net, pts in RULE_AREAS:
        z = polygon_zone(board, pts)
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
    for layer in (F, B):
        z = polygon_zone(board, [(0, 0), (W, 0), (W, H), (0, H)])
        z.SetLayer(layer)
        z.SetNet(gnd)
        z.SetZoneName("GND_" + pcbnew.BOARD.GetStandardLayerName(layer).split(".")[0])
        z.SetLocalClearance(mm(0.3))
        z.SetMinThickness(mm(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        z.SetThermalReliefGap(mm(0.4))
        z.SetThermalReliefSpokeWidth(mm(0.5))
        z.SetAssignedPriority(0)
        board.Add(z)


def power_ground_pads(fps):
    """Ground pads that carry the load or hot-loop current join the pours without thermal spokes."""
    for ref, num in [("C1", "2"), ("C2", "2"), ("Q2", "3"), ("C7", "2"), ("C8", "2"), ("J1", "2"), ("J2", "2"),
                     ("C3", "2")]:
        for pad in fps[ref].Pads():
            if pad.GetNumber() == num:
                pad.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)


def stitch(board):
    """GND vias on a STITCH_PITCH grid wherever both pours have filled copper with room for a via,
    and wherever one pour has copper the other lacks (a patch that would otherwise float)."""
    pour = {z.GetLayer(): z for z in board.Zones() if not z.GetIsRuleArea()}
    footprints = board.Footprints()
    pads = [q for i in range(len(footprints)) for q in footprints[i].Pads()]
    items = board.Tracks()
    items = [items[i] for i in range(len(items))]
    vias = [t.GetPosition() for t in items if t.GetClass() == "PCB_VIA"]
    clear = mm(0.3 + 0.45)
    added = []
    for iy in range(4, int(H * 2) - 3):
        for ix in range(4, int(W * 2) - 3):
            x, y = ix * 0.5, iy * 0.5
            q = P(x, y)
            on_f = pour[F].GetFilledPolysList(F).Contains(q)
            on_b = pour[B].GetFilledPolysList(B).Contains(q)
            if not (on_f or on_b):
                continue
            on_grid = (abs(x / STITCH_PITCH - round(x / STITCH_PITCH)) < 1e-6 and
                       abs(y / STITCH_PITCH - round(y / STITCH_PITCH)) < 1e-6)
            if on_f and on_b and not on_grid:
                continue
            if any((x - hx) ** 2 + (y - hy) ** 2 < 4.2 ** 2 for hx, hy in HOLES):
                continue
            if any(p_.HitTest(q, clear) for p_ in pads) or any(t.HitTest(q, clear) for t in items):
                continue
            if any((q.x - v.x) ** 2 + (q.y - v.y) ** 2 < mm(2.0) ** 2 for v in vias + added):
                continue
            # the fill must reach 0.65 mm round the via on both layers, or the via would sit in a gap
            ring = [P(x + dx, y + dy) for dx, dy in ((0.65, 0), (-0.65, 0), (0, 0.65), (0, -0.65))]
            if not all(pour[L].GetFilledPolysList(L).Contains(r) for L in (F, B) for r in ring) and on_f and on_b:
                continue
            added.append(q)
    gnd = board.FindNet("GND")
    for q in added:
        v = pcbnew.PCB_VIA(board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetPosition(q)
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetNet(gnd)
        v.SetLocked(True)
        board.Add(v)
    return len(added)


# ----------------------------------------------------------------- output
DRU = """(version 1)
(rule "VIN power copper in PWR_VIN at least %(m)s mm (%(w)s mm drawn, sized by trace_width.py)"
\t(condition "A.Type == 'Track' && A.NetName == '/VIN' && A.intersectsArea('PWR_VIN')")
\t(constraint track_width (min %(m)smm)))
(rule "Switch-node F.Cu in PWR_SW at least %(m)s mm"
\t(condition "A.Type == 'Track' && A.NetName == '/SW' && A.Layer == 'F.Cu' && A.intersectsArea('PWR_SW')")
\t(constraint track_width (min %(m)smm)))
(rule "Every VOUT track at least %(m)s mm"
\t(condition "A.Type == 'Track' && A.NetName == '/VOUT'")
\t(constraint track_width (min %(m)smm)))
(rule "VLOAD copper in PWR_VLOAD at least %(m)s mm"
\t(condition "A.Type == 'Track' && A.NetName == '/VLOAD' && A.intersectsArea('PWR_VLOAD')")
\t(constraint track_width (min %(m)smm)))
(rule "Hot-loop ground (GND tracks in GND_HOT) at least %(m)s mm"
\t(condition "A.Type == 'Track' && A.NetName == 'GND' && A.intersectsArea('GND_HOT')")
\t(constraint track_width (min %(m)smm)))
""" % dict(m=POWER_MIN, w=POWER_W)


def save(board):
    pcbnew.SaveBoard(PCB, board)
    with open(os.path.join(HERE, PROJECT + ".kicad_dru"), "w", encoding="utf-8", newline="\n") as f:
        f.write(DRU)


def run(cmd):
    top = os.path.dirname(os.path.dirname(HERE))
    cmd = [os.path.relpath(c, HERE) + (os.sep if c.endswith(os.sep) else "") if c.startswith(top) else c
           for c in cmd]
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=HERE, check=True)


def export():
    hw = os.path.normpath(os.path.join(HERE, ".."))
    fab = os.path.join(hw, "fab")
    docs = os.path.normpath(os.path.join(hw, "..", "docs"))
    # DRC first: any violation, unconnected pad or parity error stops here, before fab files are touched
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
        run([KICAD_CLI, "pcb", "render", "--side", side, "--width", "1600", "--height", "1200", "--zoom", "1.1",
             "--quality", "basic", "--background", "opaque", "-o", os.path.join(hw, "pcb_%s.png" % side), PCB])


def main():
    sys.stdout.reconfigure(line_buffering=True)    # keep this script's lines in order with kicad-cli's
    comps, nets = read_netlist()
    missing = sorted(set(comps) - set(PLACE))
    if missing:
        raise SystemExit("no placement for %s" % ", ".join(missing))
    board = pcbnew.NewBoard(PCB)
    pcbnew.KIID.SeedGenerator(UUID_SEED)
    setup(board)
    N = add_nets(board, nets)
    fps = place(board, comps, nets, N)
    board.SynchronizeNetsAndNetClasses(True)
    outline(board)
    power_ground_pads(fps)
    place_refs(board, fps)
    route(Copper(board, N, fps))
    rule_areas(board)
    pours(board, N["GND"])
    pcbnew.SaveBoard(PCB, board)
    stitched = 0
    while True:                          # fill on the board as saved, then stitch, until nothing is added
        board = pcbnew.LoadBoard(PCB)
        pcbnew.KIID.SeedGenerator(UUID_SEED + 1 + stitched)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        added = stitch(board)
        pcbnew.SaveBoard(PCB, board)
        stitched += added
        if not added:
            break
    print("GND stitching vias added:", stitched)
    board.BuildConnectivity()
    print("unrouted connections:", board.GetConnectivity().GetUnconnectedCount(False))
    save(board)
    print("wrote", os.path.relpath(PCB, os.path.join(HERE, "..", "..")))
    if "--export" in sys.argv[1:]:
        export()

if __name__ == "__main__":
    main()
