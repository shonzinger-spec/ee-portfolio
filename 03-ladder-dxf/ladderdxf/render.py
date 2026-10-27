"""Picture of the sheet (PNG or PDF) through ezdxf's matplotlib drawing add-on."""

from __future__ import annotations

from pathlib import Path


def render(dxf_path, out_path, dpi: int = 150) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import ezdxf
    from ezdxf.addons.drawing import matplotlib as mpl_drawing

    doc = ezdxf.readfile(dxf_path)
    mpl_drawing.qsave(doc.modelspace(), str(out_path), bg="#FFFFFF", dpi=dpi, size_inches=(17, 11))
    return Path(out_path)
