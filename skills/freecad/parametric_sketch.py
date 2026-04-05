"""
Parametric mounting bracket driven by a Spreadsheet + Sketcher constraints.

Demonstrates: arcs, tangent/perpendicular/angle constraints, radius constraints,
spreadsheet-driven parameters with aliases and formulas, and TechDraw dimensions
derived from solved sketch geometry.

Runs inside freecadcmd under xvfb (needs Qt event pump for TechDraw view computation).
Output directory is read from OUTDIR env var (default: current directory).
Produces bracket.FCStd. Use export_page.py to export to DXF/SVG/PDF.

Usage:
  OUTDIR=/tmp/out xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd parametric_sketch.py
"""

import math
import os
import sys
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


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def pump(seconds=3):
    """Process Qt events to let TechDraw background computation run."""
    for _ in range(int(seconds * 10)):
        if qapp:
            qapp.processEvents()
        time.sleep(0.1)


# === Document ===
doc = App.newDocument("BracketTest")

# === Spreadsheet Parameters ===
sheet = doc.addObject("Spreadsheet::Sheet", "Params")

# Input parameters: (row, label, value, alias)
_PARAMS = [
    (1, "Width", "120", "Width"),
    (2, "Height", "80", "Height"),
    (3, "FilletRadius", "12", "FilletRadius"),
    (4, "HoleRadius", "8", "HoleRadius"),
    (5, "TabAngle_deg", "60", "TabAngle"),
    (6, "TabLength", "35", "TabLength"),
]
for row, label, value, alias in _PARAMS:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", value)
    sheet.setAlias(f"B{row}", alias)

# Computed intermediates: (row, label, formula, alias)
_COMPUTED = [(8, "HalfWidth", "=Width / 2", "HalfWidth"), (9, "HalfHeight", "=Height / 2", "HalfHeight")]
for row, label, formula, alias in _COMPUTED:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", formula)
    sheet.setAlias(f"B{row}", alias)

doc.recompute()

# Read spreadsheet values for initial geometry placement
W = float(sheet.get("B1"))
H = float(sheet.get("B2"))
R = float(sheet.get("B3"))
HOLE_R = float(sheet.get("B4"))
TAB_ANGLE_DEG = float(sheet.get("B5"))
TAB_LEN = float(sheet.get("B6"))
TAB_ANGLE_RAD = math.radians(TAB_ANGLE_DEG)

log(f"Params: {W=}, {H=}, {R=}, {HOLE_R=}, {TAB_ANGLE_DEG=}, {TAB_LEN=}")

# === Sketch ===
# The outer profile is a single closed contour:
#   bottom-left → bottom to tab start → angled down to tab tip → vertical back up
#   to bottom level → continue bottom to right → right side → fillet arc →
#   top → fillet arc → left side → close
#
# This makes the tab an integral part of the profile, not a separate element.

sk = doc.addObject("Sketcher::SketchObject", "BracketSketch")

# Pre-compute tab geometry for initial placement
tab_start_x = W / 2
tab_tip_x = tab_start_x + TAB_LEN * math.cos(TAB_ANGLE_RAD)
tab_tip_y = -TAB_LEN * math.sin(TAB_ANGLE_RAD)

# --- Outer profile (CCW from origin) ---
bot_left = sk.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(tab_start_x, 0, 0)))
tab_down = sk.addGeometry(Part.LineSegment(App.Vector(tab_start_x, 0, 0), App.Vector(tab_tip_x, tab_tip_y, 0)))
tab_up = sk.addGeometry(Part.LineSegment(App.Vector(tab_tip_x, tab_tip_y, 0), App.Vector(tab_tip_x, 0, 0)))
bot_right = sk.addGeometry(Part.LineSegment(App.Vector(tab_tip_x, 0, 0), App.Vector(W, 0, 0)))
right = sk.addGeometry(Part.LineSegment(App.Vector(W, 0, 0), App.Vector(W, H - R, 0)))
arc_tr = sk.addGeometry(
    Part.ArcOfCircle(Part.Circle(App.Vector(W - R, H - R, 0), App.Vector(0, 0, 1), R), 0, math.pi / 2)
)
top = sk.addGeometry(Part.LineSegment(App.Vector(W - R, H, 0), App.Vector(R, H, 0)))
arc_tl = sk.addGeometry(
    Part.ArcOfCircle(Part.Circle(App.Vector(R, H - R, 0), App.Vector(0, 0, 1), R), math.pi / 2, math.pi)
)
left = sk.addGeometry(Part.LineSegment(App.Vector(0, H - R, 0), App.Vector(0, 0, 0)))
hole = sk.addGeometry(Part.Circle(App.Vector(W / 2, H / 2, 0), App.Vector(0, 0, 1), HOLE_R))

# === Constraints ===

# Chain the closed profile: Coincident at line-line junctions, Tangent at arc-line
# (Tangent with point refs implies coincidence — no separate Coincident needed).
# All chains connect end (pt 2) of one segment to start (pt 1) of the next.
for a, b in [(bot_left, tab_down), (tab_down, tab_up), (tab_up, bot_right), (bot_right, right), (left, bot_left)]:
    sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))

for a, b in [(right, arc_tr), (arc_tr, top), (top, arc_tl), (arc_tl, left)]:
    sk.addConstraint(Sketcher.Constraint("Tangent", a, 2, b, 1))

# Orientation
for geo in [bot_left, bot_right, top]:
    sk.addConstraint(Sketcher.Constraint("Horizontal", geo))
for geo in [right, left, tab_up]:
    sk.addConstraint(Sketcher.Constraint("Vertical", geo))

sk.addConstraint(Sketcher.Constraint("Equal", arc_tr, arc_tl))
sk.addConstraint(Sketcher.Constraint("Coincident", bot_left, 1, -1, 1))
# Tab returns to bottom level
sk.addConstraint(Sketcher.Constraint("Horizontal", tab_up, 2, bot_left, 1))

# --- Dimensional constraints bound to spreadsheet ---
_DIM_BINDINGS = [
    (Sketcher.Constraint("DistanceX", bot_left, 1, bot_right, 2, W), "Params.Width"),
    (Sketcher.Constraint("DistanceY", bot_left, 1, top, 1, H), "Params.Height"),
    (Sketcher.Constraint("Radius", arc_tr, R), "Params.FilletRadius"),
    (Sketcher.Constraint("Radius", hole, HOLE_R), "Params.HoleRadius"),
    (Sketcher.Constraint("DistanceX", -1, 1, hole, 3, W / 2), "Params.HalfWidth"),
    (Sketcher.Constraint("DistanceY", -1, 1, hole, 3, H / 2), "Params.HalfHeight"),
    (Sketcher.Constraint("DistanceX", -1, 1, tab_down, 1, W / 2), "Params.HalfWidth"),
    # Angle expressions need explicit unit: "deg" (raw radians treated as dimensionless)
    (Sketcher.Constraint("Angle", tab_down, -TAB_ANGLE_RAD), "-Params.TabAngle * 1 deg"),
    (Sketcher.Constraint("Distance", tab_down, 1, tab_down, 2, TAB_LEN), "Params.TabLength"),
]
for constraint, expr in _DIM_BINDINGS:
    idx = sk.addConstraint(constraint)
    sk.setExpression(f"Constraints[{idx}]", expr)

doc.recompute()
log(f"Sketch: {sk.GeometryCount} geom, {sk.ConstraintCount} constraints, FullyConstrained={sk.FullyConstrained}")
assert sk.FullyConstrained, "Sketch not fully constrained!"

# === Part Feature ===
outer_edges = [
    sk.Geometry[i].toShape() for i in [bot_left, tab_down, tab_up, bot_right, right, arc_tr, top, arc_tl, left]
]
outer_face = Part.Face(Part.Wire(outer_edges))

hole_geo = sk.Geometry[hole]
hole_edge = Part.makeCircle(hole_geo.Radius, App.Vector(hole_geo.Center.x, hole_geo.Center.y, 0))
bracket_face = outer_face.cut(Part.Face(Part.Wire([hole_edge])))

feat = doc.addObject("Part::Feature", "BracketShape")
feat.Shape = bracket_face
doc.recompute()

# === TechDraw Page ===
tmpl_path = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg")  # noqa: PTH118
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
log(f"TechDraw view: {n_edges} visible edges")
assert n_edges > 0, "TechDraw view has 0 edges — Qt event pump may have failed"

# === Dimensions ===
bb = feat.Shape.BoundBox
cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2


def vpt(sx, sy):
    """Sketch coord to view-local unscaled coord."""
    return App.Vector(sx - cx, sy - cy, 0)


# Read solved positions from sketch geometry
bot_r_end = sk.Geometry[bot_right].EndPoint
top_left_pt = sk.Geometry[top].EndPoint
hole_ctr = sk.Geometry[hole].Center
hole_r = sk.Geometry[hole].Radius
fillet_r = sk.Geometry[arc_tr].Radius
arc_tr_ctr = sk.Geometry[arc_tr].Center
tab_start_pt = sk.Geometry[tab_down].StartPoint
tab_tip_pt = sk.Geometry[tab_down].EndPoint

DIM_OFF = 18

# 1. Overall width (below the entire shape)
width_dim_y = bb.YMin - DIM_OFF
d_w = TechDraw.makeDistanceDim(view, "DistanceX", vpt(0, width_dim_y), vpt(bot_r_end.x, width_dim_y))
page.addView(d_w)
d_w.X = bot_r_end.x / 2 - cx
d_w.Y = width_dim_y - cy

# 2. Overall height (left of left edge)
d_h = TechDraw.makeDistanceDim(view, "DistanceY", vpt(-DIM_OFF, 0), vpt(-DIM_OFF, top_left_pt.y))
page.addView(d_h)
d_h.X = -DIM_OFF - 10 - cx
d_h.Y = top_left_pt.y / 2 - cy

# 3-6. Entity-referenced dimensions for fillet, hole, and tab angle.
# Edge indices vary between recomputes — identify edges by geometric properties.
vis_edges = view.getVisibleEdges()


def find_edge(predicate, desc):
    """Find the unique edge matching predicate. Raises if zero or multiple match."""
    matches = [(i, e) for i, e in enumerate(vis_edges) if predicate(e)]
    if len(matches) == 0:
        raise AssertionError(f"No edge matching: {desc}")
    if len(matches) > 1:
        raise AssertionError(f"Multiple edges matching: {desc} (got {[i for i, _ in matches]})")
    return matches[0][0]


def _edge_dx(e):
    return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)


def _edge_dy(e):
    return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)


hole_edge = find_edge(
    lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - hole_r) < 0.1, f"circle R={hole_r}"
)
# Two fillets have the same radius; pick the rightmost (top-right corner)
fillet_matches = [
    (i, e) for i, e in enumerate(vis_edges) if isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - fillet_r) < 0.1
]
assert fillet_matches, f"No arc edge matching fillet radius {fillet_r}"
fillet_edge = max(fillet_matches, key=lambda ie: ie[1].Curve.Center.x)[0]

bot_left_edge = find_edge(
    lambda e: isinstance(e.Curve, Part.Line) and _edge_dy(e) < 1 and e.Vertexes[0].Point.x < -cx + 1,
    "leftmost horizontal line",
)
tab_down_edge = find_edge(
    lambda e: isinstance(e.Curve, Part.Line) and _edge_dx(e) > 1 and _edge_dy(e) > 1, "diagonal line (tab)"
)

# 3. Fillet radius
d_fr = doc.addObject("TechDraw::DrawViewDimension", "FilletRadius")
page.addView(d_fr)
d_fr.Type = "Radius"
d_fr.References2D = [(view, f"Edge{fillet_edge}")]
d_fr.X = arc_tr_ctr.x - cx + fillet_r + 3
d_fr.Y = arc_tr_ctr.y - cy + fillet_r + 3

# 4. Hole radius
d_hr = doc.addObject("TechDraw::DrawViewDimension", "HoleRadius")
page.addView(d_hr)
d_hr.Type = "Radius"
d_hr.References2D = [(view, f"Edge{hole_edge}")]
d_hr.X = hole_ctr.x - cx + hole_r + 15
d_hr.Y = hole_ctr.y - cy

# 5. Tab length
d_tl = TechDraw.makeDistanceDim(view, "Distance", vpt(tab_start_pt.x, tab_start_pt.y), vpt(tab_tip_pt.x, tab_tip_pt.y))
page.addView(d_tl)
d_tl.X = (tab_start_pt.x + tab_tip_pt.x) / 2 - cx + 15
d_tl.Y = (tab_start_pt.y + tab_tip_pt.y) / 2 - cy - 5

# 6. Tab angle (entity-referenced between bottom edge and angled tab edge)
assert bot_left_edge is not None, "No horizontal edge found for bot_left"
assert tab_down_edge is not None, "No diagonal edge found for tab_down"
d_angle = doc.addObject("TechDraw::DrawViewDimension", "TabAngle")
page.addView(d_angle)
d_angle.Type = "Angle"
d_angle.References2D = [(view, f"Edge{bot_left_edge}"), (view, f"Edge{tab_down_edge}")]
d_angle.X = tab_start_pt.x - cx - 12
d_angle.Y = tab_start_pt.y - cy - 8

# === Annotations ===
ann_title = doc.addObject("TechDraw::DrawViewAnnotation", "Title")
page.addView(ann_title)
ann_title.Text = ["Mounting Bracket"]
ann_title.X = float(view.X)
ann_title.Y = 25
ann_title.TextSize = 6

ann_material = doc.addObject("TechDraw::DrawViewAnnotation", "Material")
page.addView(ann_material)
ann_material.Text = ["Material: Steel, 3mm"]
ann_material.X = float(view.X)
ann_material.Y = 33
ann_material.TextSize = 4

doc.recompute(None, True, True)
pump(1)

# === Save ===
fcstd_path = os.path.join(outdir, "bracket.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes")

os._exit(0)
