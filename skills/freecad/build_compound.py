"""
Build a compound shape from a wall shell and a closed rectangle, then export via TechDraw.

Demonstrates:
- Spreadsheet-driven parameters with aliases and setExpression() bindings
- Part.makeCompound for grouping multiple faces into a single Part::Feature
- Wall shell as fully constrained sketch geometry (inner + outer outlines with thickness)
- Entity-referenced TechDraw dimensions that auto-update when parameters change
- Single compound, single TechDraw view (preserves relative positions)

Runs inside freecadcmd under xvfb (needs Qt event pump for TechDraw view computation).
Output directory is read from OUTDIR env var (default: current directory).

Usage:
  OUTDIR=/tmp/out xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd build_compound.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, "/work")  # freecad_helpers.py is mounted alongside this script

import FreeCAD as App
import FreeCADGui as Gui
import Part
import Sketcher
from freecad_helpers import init_gui, log, pump, wait_for_view

Gui.showMainWindow()
qapp = init_gui()

outdir = os.environ.get("OUTDIR", ".")


# === Document ===
doc = App.newDocument("CompoundExample")

# === Spreadsheet Parameters ===
sheet = doc.addObject("Spreadsheet::Sheet", "Params")

_PARAMS = [
    (1, "RoomWidth", "4000", "RoomWidth"),
    (2, "RoomHeight", "3000", "RoomHeight"),
    (3, "TableWidth", "1200", "TableWidth"),
    (4, "TableHeight", "600", "TableHeight"),
    (5, "TableX", "500", "TableX"),
    (6, "TableY", "500", "TableY"),
    (7, "WallThickness", "150", "WallThickness"),
]
for row, label, value, alias in _PARAMS:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", value)
    sheet.setAlias(f"B{row}", alias)

# Computed intermediates
_COMPUTED = [
    (9, "RoomWidthPlusThickness", "=RoomWidth + WallThickness", "RoomWidthPlusThickness"),
    (10, "RoomHeightPlus2Thickness", "=RoomHeight + 2 * WallThickness", "RoomHeightPlus2Thickness"),
    (11, "RoomWidthMinusThickness", "=RoomWidth - WallThickness", "RoomWidthMinusThickness"),
]
for row, label, formula, alias in _COMPUTED:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", formula)
    sheet.setAlias(f"B{row}", alias)

doc.recompute()

# Read spreadsheet values for initial geometry placement
ROOM_W = float(sheet.get("B1"))
ROOM_H = float(sheet.get("B2"))
TABLE_W = float(sheet.get("B3"))
TABLE_H = float(sheet.get("B4"))
TABLE_X = float(sheet.get("B5"))
TABLE_Y = float(sheet.get("B6"))
t = float(sheet.get("B7"))

log(f"Params: {ROOM_W=}, {ROOM_H=}, {TABLE_W=}, {TABLE_H=}, {TABLE_X=}, {TABLE_Y=}, {t=}")

# === Sketch (fully constrained) ===
sk = doc.addObject("Sketcher::SketchObject", "Layout")

# Wall shell: L-shaped closed polygon with 6 vertices tracing inner and outer outlines.
# All geometry is constrained — dimensions drive the shape.
#
#    s4(Rw-t,Rh+t)──s3(Rw+t,Rh+t)
#         │              │
#         │  right wall  │
#         │              │
#    s5(Rw-t,t)          │
#         │              │
#  s6(0,t)┘              │
#    │                   │
#  s1(0,-t)──────────s2(Rw+t,-t)
#        bottom wall
#
pts = [
    App.Vector(0, -t, 0),  # s1: bottom-left outer
    App.Vector(ROOM_W + t, -t, 0),  # s2: bottom-right outer
    App.Vector(ROOM_W + t, ROOM_H + t, 0),  # s3: top-right outer
    App.Vector(ROOM_W - t, ROOM_H + t, 0),  # s4: top-right inner (cap)
    App.Vector(ROOM_W - t, t, 0),  # s5: inner L-bend
    App.Vector(0, t, 0),  # s6: bottom-left inner
]
wall_indices = []
for i in range(6):
    j = (i + 1) % 6
    idx = sk.addGeometry(Part.LineSegment(pts[i], pts[j]))
    wall_indices.append(idx)
s1, s2, s3, s4, s5, s6 = wall_indices

# Chain corners (each segment end → next segment start)
for i in range(6):
    sk.addConstraint(Sketcher.Constraint("Coincident", wall_indices[i], 2, wall_indices[(i + 1) % 6], 1))

# Orientation constraints
for i in [s1, s3, s5]:  # bottom outer, top cap, inner horizontal
    sk.addConstraint(Sketcher.Constraint("Horizontal", i))
for i in [s2, s4, s6]:  # right outer, right inner, left cap
    sk.addConstraint(Sketcher.Constraint("Vertical", i))

# Pin bottom-left outer corner (s1 start) at origin X, below origin Y by thickness
c_pinx = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, s1, 1, 0.0))
c_piny = sk.addConstraint(Sketcher.Constraint("DistanceY", s1, 1, -1, 1, t))
sk.setExpression(f"Constraints[{c_piny}]", "Params.WallThickness")

# Dimensional constraints bound to spreadsheet
_DIM_BINDINGS = [
    (Sketcher.Constraint("DistanceX", s1, 1, s1, 2, ROOM_W + t), "Params.RoomWidthPlusThickness"),
    (Sketcher.Constraint("DistanceY", s2, 1, s2, 2, ROOM_H + 2 * t), "Params.RoomHeightPlus2Thickness"),
    (Sketcher.Constraint("DistanceY", s4, 2, s4, 1, ROOM_H), "Params.RoomHeight"),
    (Sketcher.Constraint("DistanceX", s5, 2, s5, 1, ROOM_W - t), "Params.RoomWidthMinusThickness"),
]
for constraint, expr in _DIM_BINDINGS:
    idx = sk.addConstraint(constraint)
    sk.setExpression(f"Constraints[{idx}]", expr)

# Table: fully constrained rectangle
x, y, w, h = TABLE_X, TABLE_Y, TABLE_W, TABLE_H
t0 = sk.addGeometry(Part.LineSegment(App.Vector(x, y, 0), App.Vector(x + w, y, 0)))
t1 = sk.addGeometry(Part.LineSegment(App.Vector(x + w, y, 0), App.Vector(x + w, y + h, 0)))
t2 = sk.addGeometry(Part.LineSegment(App.Vector(x + w, y + h, 0), App.Vector(x, y + h, 0)))
t3 = sk.addGeometry(Part.LineSegment(App.Vector(x, y + h, 0), App.Vector(x, y, 0)))
for a, b in [(t0, t1), (t1, t2), (t2, t3), (t3, t0)]:
    sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))
for i in [t0, t2]:
    sk.addConstraint(Sketcher.Constraint("Horizontal", i))
for i in [t1, t3]:
    sk.addConstraint(Sketcher.Constraint("Vertical", i))

_TABLE_BINDINGS = [
    (Sketcher.Constraint("DistanceX", t0, 1, t0, 2, w), "Params.TableWidth"),
    (Sketcher.Constraint("DistanceY", t1, 1, t1, 2, h), "Params.TableHeight"),
    (Sketcher.Constraint("DistanceX", -1, 1, t0, 1, x), "Params.TableX"),
    (Sketcher.Constraint("DistanceY", -1, 1, t0, 1, y), "Params.TableY"),
]
for constraint, expr in _TABLE_BINDINGS:
    idx = sk.addConstraint(constraint)
    sk.setExpression(f"Constraints[{idx}]", expr)

table_indices = (t0, t1, t2, t3)

doc.recompute()
assert sk.FullyConstrained, "Sketch not fully constrained!"
log(f"Sketch: {sk.GeometryCount} geom, {sk.ConstraintCount} constraints")

# === Part Features ===
# Extract solved geometry from sketch → Part faces → compound


def sketch_face(indices):
    """Build a Part.Face from sketch geometry indices."""
    edges = [
        Part.makeLine(
            App.Vector(sk.Geometry[i].StartPoint.x, sk.Geometry[i].StartPoint.y, 0),
            App.Vector(sk.Geometry[i].EndPoint.x, sk.Geometry[i].EndPoint.y, 0),
        )
        for i in indices
    ]
    return Part.Face(Part.Wire(edges))


all_faces = [sketch_face(wall_indices), sketch_face(table_indices)]

feat = doc.addObject("Part::Feature", "AllShapes")
feat.Shape = Part.makeCompound(all_faces)
doc.recompute()
log(f"Compound: {len(all_faces)} faces")

# === TechDraw Page ===
tmpl_path = os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg")  # noqa: PTH118 — FreeCAD API expects str
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

log("recompute + wait_for_view (TechDraw HLR)")
doc.recompute(None, True, True)
wait_for_view(view, qapp)
doc.recompute(None, True, True)
pump(qapp, 0.5)

n_edges = len(view.getVisibleEdges())
log(f"TechDraw view: {n_edges} visible edges")
assert n_edges > 0, "TechDraw view has 0 edges — Qt event pump may have failed"

# === Dimensions (entity-referenced) ===
bb = feat.Shape.BoundBox
cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2
scale = float(view.Scale)

# Read solved geometry for label placement
room_w_solved = float(sheet.get("B1"))
room_h_solved = float(sheet.get("B2"))
table_w_solved = float(sheet.get("B3"))
table_h_solved = float(sheet.get("B4"))
wall_t_solved = float(sheet.get("B7"))

DIM_OFF = 0.8  # view-local offset for dimension lines (in view-scaled mm)

# Identify TechDraw edges by geometric properties.
vis_edges = view.getVisibleEdges()


def _edge_dx(e):
    return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)


def _edge_dy(e):
    return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)


def _edge_len(e):
    return ((_edge_dx(e) ** 2) + (_edge_dy(e) ** 2)) ** 0.5


def _edge_midx(e):
    return (e.Vertexes[0].Point.x + e.Vertexes[1].Point.x) / 2


def _edge_midy(e):
    return (e.Vertexes[0].Point.y + e.Vertexes[1].Point.y) / 2


def _edge_miny(e):
    return min(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y)


def _edge_minx(e):
    return min(e.Vertexes[0].Point.x, e.Vertexes[1].Point.x)


def find_edge(predicate, desc):
    """Find the unique edge matching predicate. Raises if zero or multiple match."""
    matches = [(i, e) for i, e in enumerate(vis_edges) if predicate(e)]
    if len(matches) == 0:
        raise AssertionError(f"No edge matching: {desc}")
    if len(matches) > 1:
        raise AssertionError(f"Multiple edges matching: {desc} (got {[i for i, _ in matches]})")
    return matches[0][0]


# Edge lengths in view-local coords (sketch mm * scale)
bottom_outer_len = (room_w_solved + wall_t_solved) * scale
right_outer_len = (room_h_solved + 2 * wall_t_solved) * scale
table_bot_len = table_w_solved * scale
table_right_len = table_h_solved * scale

# Bottom outer wall (longest horizontal at bottom of view)
horiz_edges = [
    (i, e) for i, e in enumerate(vis_edges) if isinstance(e.Curve, Part.Line) and _edge_dy(e) < 0.1 and _edge_dx(e) > 1
]
bottom_outer_idx = min(horiz_edges, key=lambda ie: _edge_midy(ie[1]))[0]

# Right outer wall (longest vertical at right of view)
vert_edges = [
    (i, e) for i, e in enumerate(vis_edges) if isinstance(e.Curve, Part.Line) and _edge_dx(e) < 0.1 and _edge_dy(e) > 1
]
right_outer_idx = max(vert_edges, key=lambda ie: _edge_midx(ie[1]))[0]

# Table bottom edge (horizontal, shorter than outer walls, lowest among table edges)
table_horiz = [
    (i, e)
    for i, e in enumerate(vis_edges)
    if isinstance(e.Curve, Part.Line) and _edge_dy(e) < 0.1 and abs(_edge_dx(e) - table_bot_len) < 1
]
table_bot_idx = min(table_horiz, key=lambda ie: _edge_midy(ie[1]))[0]

# Table right edge (vertical, matches table height)
table_vert = [
    (i, e)
    for i, e in enumerate(vis_edges)
    if isinstance(e.Curve, Part.Line) and _edge_dx(e) < 0.1 and abs(_edge_dy(e) - table_right_len) < 1
]
table_right_idx = min(table_vert, key=lambda ie: _edge_minx(ie[1]))[0]

# Wall thickness: find the inner horizontal edge (second-lowest horizontal, length ~ RoomW - t)
inner_horiz_len = (room_w_solved - wall_t_solved) * scale
wall_inner_horiz = [
    (i, e)
    for i, e in enumerate(vis_edges)
    if isinstance(e.Curve, Part.Line) and _edge_dy(e) < 0.1 and abs(_edge_dx(e) - inner_horiz_len) < 1
]
inner_horiz_idx = wall_inner_horiz[0][0] if wall_inner_horiz else None

# Left wall edge (leftmost vertical, full height of inner wall)
left_wall_idx = min(vert_edges, key=lambda ie: _edge_midx(ie[1]))[0]

# 1. Room width (bottom outer edge)
d_w = doc.addObject("TechDraw::DrawViewDimension", "RoomWidth")
page.addView(d_w)
d_w.Type = "DistanceX"
d_w.References2D = [(view, f"Edge{bottom_outer_idx}")]
d_w.X = 0
d_w.Y = (bb.YMin - cy) * scale - DIM_OFF

# 2. Room height (right outer edge)
d_h = doc.addObject("TechDraw::DrawViewDimension", "RoomHeight")
page.addView(d_h)
d_h.Type = "DistanceY"
d_h.References2D = [(view, f"Edge{right_outer_idx}")]
d_h.X = (bb.XMax - cx) * scale + DIM_OFF + 0.5
d_h.Y = 0

# 3. Table width (table bottom edge)
d_tw = doc.addObject("TechDraw::DrawViewDimension", "TableWidth")
page.addView(d_tw)
d_tw.Type = "DistanceX"
d_tw.References2D = [(view, f"Edge{table_bot_idx}")]
d_tw.X = _edge_midx(vis_edges[table_bot_idx])
d_tw.Y = _edge_midy(vis_edges[table_bot_idx]) - DIM_OFF * 0.6

# 4. Table height (table right edge)
d_th = doc.addObject("TechDraw::DrawViewDimension", "TableHeight")
page.addView(d_th)
d_th.Type = "DistanceY"
d_th.References2D = [(view, f"Edge{table_right_idx}")]
d_th.X = _edge_midx(vis_edges[table_right_idx]) + DIM_OFF * 0.6
d_th.Y = _edge_midy(vis_edges[table_right_idx])

# 5. Wall thickness (between bottom outer and inner horizontal edges)
if inner_horiz_idx is not None:
    d_wt = doc.addObject("TechDraw::DrawViewDimension", "WallThickness")
    page.addView(d_wt)
    d_wt.Type = "DistanceY"
    d_wt.References2D = [(view, f"Edge{bottom_outer_idx}"), (view, f"Edge{inner_horiz_idx}")]
    d_wt.X = (bb.XMin - cx) * scale - DIM_OFF
    d_wt.Y = ((bb.YMin + wall_t_solved) - cy) * scale

log("recompute after dimensions")
doc.recompute(None, True, True)
pump(qapp, 0.5)

# === Save ===
log("saving FCStd")
fcstd_path = os.path.join(outdir, "compound.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes — done")

os._exit(0)  # Qt6 TLS crash during shutdown — see debug/qt_shutdown_segfault.md
