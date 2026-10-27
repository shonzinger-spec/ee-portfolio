"""NEMA/JIC symbol blocks, the title block, and where their attributes sit (inches).

A schematic symbol's base point is its left terminal; its right terminal is CELL_W to the right.
Attribute names follow AutoCAD Electrical habits (TAG1 / TAG2, DESC1, XREF, XREFNO / XREFNC,
MFG, CAT, TERM01 / TERM02) so the data reads naturally there, but these are plain DXF blocks,
not AutoCAD Electrical components.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ezdxf.enums import TextEntityAlignment as Align

CELL_W = 0.5
TB_W, TB_H = 6.5, 1.75


@dataclass(frozen=True)
class Attr:
    tag: str
    x: float
    y: float
    height: float
    layer: str
    align: Align = Align.MIDDLE_CENTER
    invisible: bool = False


_TERMS = (Attr("TERM01", -0.07, 0.07, 0.06, "TERMS"), Attr("TERM02", CELL_W + 0.07, 0.07, 0.06, "TERMS"))
PARENT = (
    Attr("TAG1", 0.25, 0.34, 0.10, "TAGS"),
    Attr("DESC1", 0.25, -0.27, 0.08, "DESC"),
    *_TERMS,
    Attr("MFG", 0.25, -0.42, 0.06, "MFGCAT", invisible=True),
    Attr("CAT", 0.25, -0.50, 0.06, "MFGCAT", invisible=True),
)
CHILD = (Attr("TAG2", 0.25, 0.34, 0.10, "TAGS"), Attr("XREF", 0.25, -0.27, 0.08, "XREF"), *_TERMS)
COIL = (
    *PARENT,
    Attr("XREFNO", 0.75, 0.08, 0.08, "XREF", Align.MIDDLE_LEFT),  # moved to the right of L2 when inserted
    Attr("XREFNC", 0.75, -0.08, 0.08, "XREF", Align.MIDDLE_LEFT),
)
LIGHT = (*PARENT, Attr("COLOR", 0.25, 0.0, 0.12, "SYMS"))

_TB_CELLS = (("DWG NO", "DWGNO", 1.6), ("SHEET", "SHEET", 0.8), ("REV", "REV", 0.6), ("DATE", "DATE", 1.2), ("DRAWN", "DRAWN", 1.3), ("SCALE", "SCALE", 1.0))
_TB_X = [sum(cell[2] for cell in _TB_CELLS[:i]) for i in range(len(_TB_CELLS))]  # left edge of each bottom cell
TITLE = (
    Attr("PROJECT", TB_W / 2, 1.47, 0.12, "TITLE"),
    Attr("TITLE1", TB_W / 2, 0.93, 0.16, "TITLE"),
    Attr("TITLE2", TB_W / 2, 0.66, 0.11, "TITLE"),
    *(Attr(tag, x + width / 2, 0.2, 0.11, "TITLE") for (_, tag, width), x in zip(_TB_CELLS, _TB_X)),
)


def _lines(blk, *segments) -> None:
    for p1, p2 in segments:
        blk.add_line(p1, p2)


def _contact(blk, closed: bool) -> None:
    _lines(blk, ((0, 0), (0.19, 0)), ((0.31, 0), (CELL_W, 0)), ((0.19, -0.12), (0.19, 0.12)), ((0.31, -0.12), (0.31, 0.12)))
    if closed:
        _lines(blk, ((0.13, -0.14), (0.37, 0.14)))


def _pushbutton(blk, closed: bool) -> None:
    _lines(blk, ((0, 0), (0.1, 0)), ((0.4, 0), (CELL_W, 0)))
    for cx in (0.13, 0.37):
        blk.add_circle((cx, 0), 0.03)
    bar = -0.04 if closed else 0.1  # NC: bridge rests under the terminals; NO: bridge held off above them
    _lines(blk, ((0.08, bar), (0.42, bar)), ((0.25, bar), (0.25, 0.24)), ((0.18, 0.24), (0.32, 0.24)))


def _coil(blk) -> None:
    _lines(blk, ((0, 0), (0.1, 0)), ((0.4, 0), (CELL_W, 0)))
    blk.add_circle((0.25, 0), 0.15)


def _light(blk) -> None:
    _lines(blk, ((0, 0), (0.12, 0)), ((0.38, 0), (CELL_W, 0)))
    blk.add_circle((0.25, 0), 0.13)
    for deg in (45, 135, 225, 315):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        _lines(blk, ((0.25 + 0.16 * c, 0.16 * s), (0.25 + 0.23 * c, 0.23 * s)))


def _dot(blk) -> None:
    blk.add_circle((0, 0), 0.035)
    blk.add_hatch(color=256).paths.add_edge_path().add_arc((0, 0), 0.035, 0, 360)


def _label(blk, text: str, x: float, y: float = 0.42) -> None:
    blk.add_text(text, height=0.06, dxfattribs={"layer": "TITLE"}).set_placement((x + 0.06, y), align=Align.MIDDLE_LEFT)


def _title_block(blk) -> None:
    blk.add_lwpolyline([(0, 0), (TB_W, 0), (TB_W, TB_H), (0, TB_H)], close=True)
    _lines(blk, ((0, 0.5), (TB_W, 0.5)), ((0, 1.25), (TB_W, 1.25)), *(((x, 0), (x, 0.5)) for x in _TB_X[1:]))
    for (label, _, _), x in zip(_TB_CELLS, _TB_X):
        _label(blk, label, x)
    _label(blk, "TITLE", 0.0, 1.17)
    _label(blk, "PROJECT", 0.0, 1.67)


BLOCKS = {
    "NEMA_PB_NO": (lambda b: _pushbutton(b, closed=False), PARENT),
    "NEMA_PB_NC": (lambda b: _pushbutton(b, closed=True), PARENT),
    "NEMA_CONTACT_NO": (lambda b: _contact(b, closed=False), CHILD),
    "NEMA_CONTACT_NC": (lambda b: _contact(b, closed=True), CHILD),
    "NEMA_OL_NC": (lambda b: _contact(b, closed=True), PARENT),
    "NEMA_COIL": (_coil, COIL),
    "NEMA_PILOT_LIGHT": (_light, LIGHT),
    "WIRENO": (lambda b: None, (Attr("WIRENO", 0.0, 0.0, 0.09, "WIRENO"),)),
    "DOT": (_dot, ()),
    "TITLE_BLOCK": (_title_block, TITLE),
}


def define_blocks(doc) -> None:
    for name, (draw, attrs) in BLOCKS.items():
        blk = doc.blocks.new(name=name)
        draw(blk)
        for a in attrs:
            attdef = blk.add_attdef(a.tag, (a.x, a.y), height=a.height, dxfattribs={"layer": a.layer, "flags": int(a.invisible)})
            attdef.set_placement((a.x, a.y), align=a.align)


def attach_attribs(ref, values: dict[str, str], moves=None) -> None:
    """One ATTRIB per ATTDEF of the block, placed like the ATTDEF unless `moves` gives {tag: (point, align)}."""
    ix, iy = ref.dxf.insert.x, ref.dxf.insert.y
    for a in BLOCKS[ref.dxf.name][1]:
        point, align = (moves or {}).get(a.tag, ((ix + a.x, iy + a.y), a.align))
        attrib = ref.add_attrib(a.tag, values.get(a.tag, ""), dxfattribs={"layer": a.layer, "height": a.height, "flags": int(a.invisible)})
        attrib.set_placement(point, align=align)
