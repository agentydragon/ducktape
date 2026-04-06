"""
Export all TechDraw pages from an FCStd to DXF, SVG, and PDF.

Runs under the FreeCAD GUI binary (not freecadcmd). Work is deferred until after
QApplication::exec() starts via QTimer.singleShot(0, ...) — see debug/qt_shutdown_segfault.md.
Arguments via env vars (freecad treats CLI args as files to open):

  Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
  DISPLAY=:99 INPUT=rect.FCStd OUTDIR=./out freecad export_page.py

Produces <stem>.dxf, <stem>.svg, <stem>.pdf in OUTDIR,
where <stem> is the input filename without extension (e.g. rect.FCStd → rect.{dxf,svg,pdf}).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

fcstd_path = os.environ.get("INPUT")
outdir = os.environ.get("OUTDIR")
if not fcstd_path or not outdir:
    print("ERROR: INPUT and OUTDIR env vars are required")
    sys.exit(1)
stem = Path(fcstd_path).stem

import FreeCAD as App  # noqa: E402 — must parse args before FreeCAD import
import FreeCADGui as Gui  # noqa: E402
import TechDraw  # noqa: E402
import TechDrawGui  # noqa: E402
from freecad_helpers import init_gui, log, pump, wait_for_view  # noqa: E402

try:
    from PySide6.QtCore import QTimer
except ImportError:
    from PySide2.QtCore import QTimer

qapp = init_gui()


def _work() -> None:
    log("opening document")
    doc = App.openDocument(fcstd_path)

    # Find a DrawViewPart to poll for readiness
    view_part = None
    for obj in doc.Objects:
        if "DrawViewPart" in obj.TypeId:
            view_part = obj
            break

    log("recompute + wait_for_view (TechDraw HLR)")
    doc.recompute(None, True, True)
    if view_part:
        wait_for_view(view_part, qapp)
    else:
        pump(qapp, 5)
    doc.recompute(None, True, True)
    pump(qapp, 0.5)

    # Find the TechDraw page
    page = None
    for obj in doc.Objects:
        if obj.TypeId == "TechDraw::DrawPage":
            page = obj
            break

    if not page:
        print("ERROR: No TechDraw::DrawPage found")
        sys.exit(1)

    # Report view edge counts
    for obj in doc.Objects:
        if "DrawViewPart" in obj.TypeId:
            log(f"{obj.Name}: {len(obj.getVisibleEdges())} edges")

    # Export all three formats
    log("exporting DXF")
    dxf_out = os.path.join(outdir, f"{stem}.dxf")  # noqa: PTH118 — FreeCAD API expects str
    TechDraw.writeDXFPage(page, dxf_out)
    log(f"DXF: {Path(dxf_out).stat().st_size} bytes")

    log("exporting SVG")
    svg_out = os.path.join(outdir, f"{stem}.svg")  # noqa: PTH118 — FreeCAD API expects str
    TechDrawGui.exportPageAsSvg(page, svg_out)
    log(f"SVG: {Path(svg_out).stat().st_size} bytes")

    log("exporting PDF")
    pdf_out = os.path.join(outdir, f"{stem}.pdf")  # noqa: PTH118 — FreeCAD API expects str
    TechDrawGui.exportPageAsPdf(page, pdf_out)
    log(f"PDF: {Path(pdf_out).stat().st_size} bytes — done")

    doc.setClosable(True)
    App.closeDocument(doc.Name)
    Gui.getMainWindow().close()


# Defer work until after QApplication::exec() starts — the GUI binary enters
# exec() after processCmdLineFiles() returns, so module-level code here runs
# before the event loop is live. See debug/qt_shutdown_segfault.md.
QTimer.singleShot(0, _work)
