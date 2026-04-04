"""
Export FCStd TechDraw page to DXF, SVG, and PDF.

All formats require GUI mode (xvfb) for TechDraw view computation.

Usage:
  xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd export_page.py <input.FCStd> <output_dir>

Produces <output_dir>/page.dxf, <output_dir>/page.svg, <output_dir>/page.pdf.
"""

import os
import sys
import time
from pathlib import Path

args = list(sys.argv[1:])
fcstd_path = args[0] if len(args) > 0 else "input.FCStd"
outdir = args[1] if len(args) > 1 else "."

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
dxf_path = os.path.join(outdir, "page.dxf")  # noqa: PTH118 — FreeCAD API expects str
TechDraw.writeDXFPage(page, dxf_path)
print(f"DXF: {Path(dxf_path).stat().st_size} bytes")

svg_path = os.path.join(outdir, "page.svg")  # noqa: PTH118 — FreeCAD API expects str
TechDrawGui.exportPageAsSvg(page, svg_path)
print(f"SVG: {Path(svg_path).stat().st_size} bytes")

pdf_path = os.path.join(outdir, "page.pdf")  # noqa: PTH118 — FreeCAD API expects str
TechDrawGui.exportPageAsPdf(page, pdf_path)
print(f"PDF: {Path(pdf_path).stat().st_size} bytes")

os._exit(0)  # Skip Qt cleanup to avoid segfault under xvfb
