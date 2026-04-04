"""
Export FCStd TechDraw page to DXF, SVG, or PDF.

All formats require GUI mode (xvfb) for TechDraw view computation.

Usage:
  xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd export_page.py <input.FCStd> <output.{dxf,svg,pdf}>

The output format is determined by the file extension.
"""

import os
import sys
import time
from pathlib import Path

args = list(sys.argv[1:])
fcstd_path = args[0] if len(args) > 0 else "input.FCStd"
output_path = args[1] if len(args) > 1 else "output.dxf"

suffix = Path(output_path).suffix.lower()
if suffix not in (".dxf", ".svg", ".pdf"):
    print(f"ERROR: Unsupported format {suffix!r}. Use .dxf, .svg, or .pdf")
    sys.exit(1)

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

if suffix == ".dxf":
    TechDraw.writeDXFPage(page, output_path)
elif suffix == ".svg":
    TechDrawGui.exportPageAsSvg(page, output_path)
elif suffix == ".pdf":
    TechDrawGui.exportPageAsPdf(page, output_path)

print(f"Exported: {output_path} ({Path(output_path).stat().st_size} bytes)")

os._exit(0)  # Skip Qt cleanup to avoid segfault under xvfb
