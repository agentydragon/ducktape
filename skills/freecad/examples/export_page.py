"""
Export all TechDraw pages from an FCStd to DXF, SVG, and PDF.

Runs under the FreeCAD GUI binary with Xvfb. Arguments via env vars
(the freecad binary treats CLI args as files to open):

  INPUT=rect.FCStd OUTDIR=./out freecad export_page.py

Produces <stem>.dxf, <stem>.svg, <stem>.pdf in OUTDIR,
where <stem> is the input filename without extension (e.g. rect.FCStd → rect.{dxf,svg,pdf}).
"""

import os
import sys
from pathlib import Path

import FreeCAD

from skills.freecad.examples.freecad_helpers import init_gui, log, pump, run_gui_script, wait_for_view

fcstd_path = os.environ["INPUT"]
outdir = os.environ["OUTDIR"]
stem = Path(fcstd_path).stem

qapp = init_gui()


def _main() -> None:
    import TechDraw  # noqa: PLC0415 — import after exec() for safety
    import TechDrawGui  # noqa: PLC0415

    log("opening document")
    doc = FreeCAD.openDocument(fcstd_path)

    view_part = next((o for o in doc.Objects if "DrawViewPart" in o.TypeId), None)

    log("recompute + wait_for_view (TechDraw HLR)")
    doc.recompute(None, True, True)
    if view_part:
        wait_for_view(view_part, qapp)
    else:
        pump(qapp, 5)
    doc.recompute(None, True, True)
    pump(qapp, 0.5)

    page = next((o for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"), None)
    if not page:
        print("ERROR: No TechDraw::DrawPage found")
        sys.exit(1)

    for obj in doc.Objects:
        if "DrawViewPart" in obj.TypeId:
            log(f"{obj.Name}: {len(obj.getVisibleEdges())} edges")

    log("exporting DXF")
    dxf_out = os.path.join(outdir, f"{stem}.dxf")  # noqa: PTH118 — FreeCAD API expects str
    TechDraw.writeDXFPage(page, dxf_out)
    log(f"DXF: {Path(dxf_out).stat().st_size} bytes")

    log("exporting SVG")
    svg_out = os.path.join(outdir, f"{stem}.svg")  # noqa: PTH118
    TechDrawGui.exportPageAsSvg(page, svg_out)
    log(f"SVG: {Path(svg_out).stat().st_size} bytes")

    log("exporting PDF")
    pdf_out = os.path.join(outdir, f"{stem}.pdf")  # noqa: PTH118
    TechDrawGui.exportPageAsPdf(page, pdf_out)
    log(f"PDF: {Path(pdf_out).stat().st_size} bytes — done")


run_gui_script(qapp, _main)
