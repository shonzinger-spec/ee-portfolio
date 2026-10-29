"""Command line: python -m ladderdxf DEVICE_LIST [--out DIR] [--render png|pdf|none] [title options]."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .drawing import TitleInfo, write_dxf
from .model import FIRST_LINE, InputError, load_ladder
from .reports import write_bom, write_wire_list


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ladderdxf",
        description="Draw a NEMA/JIC motor-starter ladder (DXF) with a BOM and wire list (XLSX) from a device list.",
    )
    p.add_argument("input", type=Path, help="device list (.csv or .xlsx)")
    p.add_argument("--out", type=Path, default=Path("out"), help="output folder (default: out)")
    p.add_argument("--render", choices=("png", "pdf", "none"), default="png", help="also save a picture of the sheet (needs matplotlib)")
    p.add_argument("--project", default="EE PORTFOLIO", help="title block: project line")
    p.add_argument("--title", default="MOTOR STARTER", help="title block: title")
    p.add_argument("--subtitle", default="CONTROL SCHEMATIC", help="title block: second title line")
    p.add_argument("--dwg-no", default="LDX-001", help="title block: drawing number")
    p.add_argument("--rev", default="A", help="title block: revision")
    p.add_argument("--drawn-by", default="", help="title block: drawn by")
    p.add_argument("--date", default=None, help="title block: date (default: today)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    title = TitleInfo(
        project=args.project, title=args.title, subtitle=args.subtitle, dwg_no=args.dwg_no,
        rev=args.rev, drawn=args.drawn_by, date=args.date or date.today().isoformat(),
    )
    try:
        ladder = load_ladder(args.input)
        args.out.mkdir(parents=True, exist_ok=True)
        written = [write_dxf(ladder, args.out / f"{args.input.stem}.dxf", title)]
    except InputError as exc:
        print(f"ladderdxf: {exc}", file=sys.stderr)
        return 2
    written += [write_bom(ladder, args.out / f"{args.input.stem}_bom.xlsx"),
                write_wire_list(ladder, args.out / f"{args.input.stem}_wirelist.xlsx")]
    if args.render != "none":
        try:
            from .render import render

            written.append(render(written[0], args.out / f"{args.input.stem}.{args.render}"))
        except ImportError as exc:
            print(f"ladderdxf: cannot render ({exc}); pip install matplotlib, or use --render none", file=sys.stderr)
            return 3
    print(f"{len(ladder.devices)} devices, {len(ladder.wires)} wires, lines {FIRST_LINE}-{ladder.last_line}")
    for path in written:
        print(f"  wrote {path}")
    return 0
