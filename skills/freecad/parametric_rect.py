"""
Create a parameterized 10x20 cm rectangle with TechDraw dimensions.

Runs inside freecadcmd under xvfb (needs Qt event pump for TechDraw view computation).
Output directory is read from OUTDIR env var (default: current directory).
Produces rect.FCStd. Use export_page.py to export to DXF/SVG/PDF.

Usage:
  OUTDIR=/tmp/out xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd parametric_rect.py
"""

import os
import time
from pathlib import Path

import FreeCAD as App
import FreeCADGui as Gui
import Part
import Sketcher

Gui.showMainWindow()

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets

qapp = QtWidgets.QApplication.instance()
import TechDraw  # noqa: E402

outdir = os.environ.get("OUTDIR", ".")

# === Parameters ===
WIDTH = 100.0  # mm (10 cm)
HEIGHT = 200.0  # mm (20 cm)


def pump(seconds=3):
    """Process Qt events to let TechDraw background computation run."""
    for _ in range(int(seconds * 10)):
        if qapp:
            qapp.processEvents()
        time.sleep(0.1)


# === Sketch ===
doc = App.newDocument("RectTest")
sk = doc.addObject("Sketcher::SketchObject", "RectSketch")

# Rectangle: 4 line segments
i0 = sk.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(WIDTH, 0, 0)))
i1 = sk.addGeometry(Part.LineSegment(App.Vector(WIDTH, 0, 0), App.Vector(WIDTH, HEIGHT, 0)))
i2 = sk.addGeometry(Part.LineSegment(App.Vector(WIDTH, HEIGHT, 0), App.Vector(0, HEIGHT, 0)))
i3 = sk.addGeometry(Part.LineSegment(App.Vector(0, HEIGHT, 0), App.Vector(0, 0, 0)))

# Chain corners
for a, b in [(i0, i1), (i1, i2), (i2, i3), (i3, i0)]:
    sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))

# Orientation
for i in [i0, i2]:
    sk.addConstraint(Sketcher.Constraint("Horizontal", i))
for i in [i1, i3]:
    sk.addConstraint(Sketcher.Constraint("Vertical", i))

# Dimensional constraints
sk.addConstraint(Sketcher.Constraint("DistanceX", i0, 1, i0, 2, WIDTH))
sk.addConstraint(Sketcher.Constraint("DistanceY", i1, 1, i1, 2, HEIGHT))

# Pin to origin
sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, i0, 1, 0.0))
sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, i0, 1, 0.0))

doc.recompute()
assert sk.FullyConstrained, "Sketch not fully constrained!"
print(f"Sketch: {sk.GeometryCount} geom, {sk.ConstraintCount} constraints")

# === Part Feature ===
edges = [
    Part.makeLine(
        App.Vector(sk.Geometry[i].StartPoint.x, sk.Geometry[i].StartPoint.y, 0),
        App.Vector(sk.Geometry[i].EndPoint.x, sk.Geometry[i].EndPoint.y, 0),
    )
    for i in [i0, i1, i2, i3]
]
feat = doc.addObject("Part::Feature", "RectFace")
feat.Shape = Part.Face(Part.Wire(edges))
doc.recompute()

# === TechDraw Page ===
tmpl_path = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "A4_Landscape_blank.svg")  # noqa: PTH118 — FreeCAD API expects str
page = doc.addObject("TechDraw::DrawPage", "Page")
tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
tmpl.Template = tmpl_path
page.Template = tmpl

view = doc.addObject("TechDraw::DrawViewPart", "TopView")
page.addView(view)
view.Source = [feat]
view.Direction = App.Vector(0, 0, 1)
view.Scale = 1.0
view.X = 150
view.Y = 120

doc.recompute(None, True, True)
pump(5)
doc.recompute(None, True, True)
pump(2)

n_edges = len(view.getVisibleEdges())
print(f"TechDraw view: {n_edges} visible edges")
assert n_edges > 0, "TechDraw view has 0 edges — Qt event pump may have failed"

# === Dimensions ===
# Points are unscaled 2D view-local coords (sketch coords minus shape bbox center)
bb = feat.Shape.BoundBox
cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2

# Width dimension (below bottom edge)
d1 = TechDraw.makeDistanceDim(view, "DistanceX", App.Vector(0 - cx, -15 - cy, 0), App.Vector(WIDTH - cx, -15 - cy, 0))
if d1:
    page.addView(d1)
    d1.X = 0
    d1.Y = -15 - cy

# Height dimension (right of right edge)
DIM_OFFSET = 15  # mm from rectangle edge to dimension line
TEXT_OFFSET = 10  # mm from dimension line to text center
d2 = TechDraw.makeDistanceDim(
    view,
    "DistanceY",
    App.Vector(WIDTH + DIM_OFFSET - cx, 0 - cy, 0),
    App.Vector(WIDTH + DIM_OFFSET - cx, HEIGHT - cy, 0),
)
if d2:
    page.addView(d2)
    d2.X = WIDTH + DIM_OFFSET + TEXT_OFFSET - cx
    d2.Y = 0

doc.recompute(None, True, True)
pump(1)

# === Save ===
fcstd_path = os.path.join(outdir, "rect.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
print(f"FCStd: {Path(fcstd_path).stat().st_size} bytes")

os._exit(0)  # Skip Qt cleanup to avoid potential segfault under xvfb
