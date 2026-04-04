"""
Export all TechDraw pages from an FCStd to DXF, SVG, and PDF.

Requires GUI mode (xvfb) for TechDraw view computation.
Arguments via env vars (freecadcmd treats CLI args as files to open):

  INPUT=rect.FCStd OUTDIR=./out xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd export_page.py

Produces <stem>.dxf, <stem>.svg, <stem>.pdf in OUTDIR,
where <stem> is the input filename without extension (e.g. rect.FCStd → rect.{dxf,svg,pdf}).
"""

import os
import sys
import time
from pathlib import Path

fcstd_path = os.environ.get("INPUT")
outdir = os.environ.get("OUTDIR")
if not fcstd_path or not outdir:
    print("ERROR: INPUT and OUTDIR env vars are required")
    sys.exit(1)
stem = Path(fcstd_path).stem

import FreeCAD as App  # noqa: E402 — must parse args before FreeCAD import
import FreeCADGui as Gui  # noqa: E402

Gui.showMainWindow()

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets

qapp = QtWidgets.QApplication.instance()


def pump(seconds=3):
    for _ in range(int(seconds * 10)):
        if qapp:
            qapp.processEvents()
        time.sleep(0.1)


import TechDraw  # noqa: E402
import TechDrawGui  # noqa: E402

doc = App.openDocument(fcstd_path)
doc.recompute(None, True, True)
pump(5)
doc.recompute(None, True, True)
pump(2)

# Find the TechDraw page
# TODO: use more_itertools.one() once available in FreeCAD container
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
        print(f"{obj.Name}: {len(obj.getVisibleEdges())} edges")

# Export all three formats
dxf_path = os.path.join(outdir, f"{stem}.dxf")  # noqa: PTH118 — FreeCAD API expects str
TechDraw.writeDXFPage(page, dxf_path)
print(f"DXF: {Path(dxf_path).stat().st_size} bytes")

svg_path = os.path.join(outdir, f"{stem}.svg")  # noqa: PTH118 — FreeCAD API expects str
TechDrawGui.exportPageAsSvg(page, svg_path)
print(f"SVG: {Path(svg_path).stat().st_size} bytes")

pdf_path = os.path.join(outdir, f"{stem}.pdf")  # noqa: PTH118 — FreeCAD API expects str
TechDrawGui.exportPageAsPdf(page, pdf_path)
print(f"PDF: {Path(pdf_path).stat().st_size} bytes")

os._exit(0)  # Skip Qt cleanup to avoid segfault under xvfb
