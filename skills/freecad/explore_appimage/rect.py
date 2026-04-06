"""
Minimal FreeCAD script: create a 100x50 rectangle and export to DXF.

Designed to run headlessly (no TechDraw, no GUI). Tested with:
  freecadcmd rect.py

Output written to OUTDIR env var (default: /tmp).
"""

import os
from pathlib import Path

import FreeCAD as App
import importDXF
import Part

outdir = Path(os.environ.get("OUTDIR", "/tmp"))
outdir.mkdir(parents=True, exist_ok=True)

doc = App.newDocument("RectTest")

# A closed rectangular wire: 100 x 50
pts = [
    App.Vector(0, 0, 0),
    App.Vector(100, 0, 0),
    App.Vector(100, 50, 0),
    App.Vector(0, 50, 0),
    App.Vector(0, 0, 0),  # close
]
wire = Part.makePolygon(pts)
face = Part.Face(wire)

feat = doc.addObject("Part::Feature", "Rect")
feat.Shape = face
doc.recompute()

out_dxf = str(outdir / "rect.dxf")
importDXF.export([feat], out_dxf)
print(f"Wrote {out_dxf} ({Path(out_dxf).stat().st_size} bytes)")

out_fcstd = str(outdir / "rect.FCStd")
doc.saveAs(out_fcstd)
print(f"Wrote {out_fcstd} ({Path(out_fcstd).stat().st_size} bytes)")

os._exit(0)
