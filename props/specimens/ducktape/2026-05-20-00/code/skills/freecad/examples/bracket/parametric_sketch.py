"""
Parametric mounting bracket driven by a Spreadsheet + Sketcher constraints.

Demonstrates: arcs, tangent/perpendicular/angle constraints, radius constraints,
spreadsheet-driven parameters with aliases and formulas, and TechDraw dimensions
with entity references that update automatically.

TechDraw HLR requires Xvfb. Run via the freecad GUI binary with Xvfb:
  OUTDIR=/tmp/out freecad parametric_sketch.py

Output directory is read from OUTDIR env var (default: current directory).
Produces bracket.FCStd. Use export_page.py to export to DXF/SVG/PDF.
"""

import math
import os
from pathlib import Path

import FreeCAD
import Part
import Sketcher

from skills.freecad.examples.freecad_helpers import init_gui, log, pump, run_gui_script, wait_for_view

outdir = os.environ.get("OUTDIR", ".")

qapp = init_gui()


def _main() -> None:
    # === Document ===
    doc = FreeCAD.newDocument("BracketTest")

    # === Spreadsheet Parameters ===
    sheet = doc.addObject("Spreadsheet::Sheet", "Params")

    # Input parameters: (row, label, value, alias)
    params = [
        (1, "Width", "120", "Width"),
        (2, "Height", "80", "Height"),
        (3, "FilletRadius", "12", "FilletRadius"),
        (4, "HoleRadius", "8", "HoleRadius"),
        (5, "TabAngle_deg", "60", "TabAngle"),
        (6, "TabLength", "35", "TabLength"),
    ]
    for row, label, value, alias in params:
        sheet.set(f"A{row}", label)
        sheet.set(f"B{row}", value)
        sheet.setAlias(f"B{row}", alias)

    # Computed intermediates: (row, label, formula, alias)
    computed = [(8, "HalfWidth", "=Width / 2", "HalfWidth"), (9, "HalfHeight", "=Height / 2", "HalfHeight")]
    for row, label, formula, alias in computed:
        sheet.set(f"A{row}", label)
        sheet.set(f"B{row}", formula)
        sheet.setAlias(f"B{row}", alias)

    doc.recompute()

    # Read spreadsheet values for initial geometry placement
    w = float(sheet.get("B1"))
    h = float(sheet.get("B2"))
    r = float(sheet.get("B3"))
    hole_r = float(sheet.get("B4"))
    tab_angle_deg = float(sheet.get("B5"))
    tab_len = float(sheet.get("B6"))
    tab_angle_rad = math.radians(tab_angle_deg)

    log(f"Params: {w=}, {h=}, {r=}, {hole_r=}, {tab_angle_deg=}, {tab_len=}")

    # === Sketch ===
    # The outer profile is a single closed contour:
    #   bottom-left → bottom to tab start → angled down to tab tip → vertical back up
    #   to bottom level → continue bottom to right → right side → fillet arc →
    #   top → fillet arc → left side → close
    #
    # This makes the tab an integral part of the profile, not a separate element.

    sk = doc.addObject("Sketcher::SketchObject", "BracketSketch")

    # Pre-compute tab geometry for initial placement
    tab_start_x = w / 2
    tab_tip_x = tab_start_x + tab_len * math.cos(tab_angle_rad)
    tab_tip_y = -tab_len * math.sin(tab_angle_rad)

    # --- Outer profile (CCW from origin) ---
    bot_left = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(tab_start_x, 0, 0)))
    tab_down = sk.addGeometry(
        Part.LineSegment(FreeCAD.Vector(tab_start_x, 0, 0), FreeCAD.Vector(tab_tip_x, tab_tip_y, 0))
    )
    tab_up = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(tab_tip_x, tab_tip_y, 0), FreeCAD.Vector(tab_tip_x, 0, 0)))
    bot_right = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(tab_tip_x, 0, 0), FreeCAD.Vector(w, 0, 0)))
    right = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(w, 0, 0), FreeCAD.Vector(w, h - r, 0)))
    arc_tr = sk.addGeometry(
        Part.ArcOfCircle(Part.Circle(FreeCAD.Vector(w - r, h - r, 0), FreeCAD.Vector(0, 0, 1), r), 0, math.pi / 2)
    )
    top = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(w - r, h, 0), FreeCAD.Vector(r, h, 0)))
    arc_tl = sk.addGeometry(
        Part.ArcOfCircle(Part.Circle(FreeCAD.Vector(r, h - r, 0), FreeCAD.Vector(0, 0, 1), r), math.pi / 2, math.pi)
    )
    left = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(0, h - r, 0), FreeCAD.Vector(0, 0, 0)))
    hole = sk.addGeometry(Part.Circle(FreeCAD.Vector(w / 2, h / 2, 0), FreeCAD.Vector(0, 0, 1), hole_r))

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
    dim_bindings = [
        (Sketcher.Constraint("DistanceX", bot_left, 1, bot_right, 2, w), "Params.Width"),
        (Sketcher.Constraint("DistanceY", bot_left, 1, top, 1, h), "Params.Height"),
        (Sketcher.Constraint("Radius", arc_tr, r), "Params.FilletRadius"),
        (Sketcher.Constraint("Radius", hole, hole_r), "Params.HoleRadius"),
        (Sketcher.Constraint("DistanceX", -1, 1, hole, 3, w / 2), "Params.HalfWidth"),
        (Sketcher.Constraint("DistanceY", -1, 1, hole, 3, h / 2), "Params.HalfHeight"),
        (Sketcher.Constraint("DistanceX", -1, 1, tab_down, 1, w / 2), "Params.HalfWidth"),
        # Angle expressions need explicit unit: "deg" (raw radians treated as dimensionless)
        (Sketcher.Constraint("Angle", tab_down, -tab_angle_rad), "-Params.TabAngle * 1 deg"),
        (Sketcher.Constraint("Distance", tab_down, 1, tab_down, 2, tab_len), "Params.TabLength"),
    ]
    for constraint, expr in dim_bindings:
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
    hole_edge = Part.makeCircle(hole_geo.Radius, FreeCAD.Vector(hole_geo.Center.x, hole_geo.Center.y, 0))
    bracket_face = outer_face.cut(Part.Face(Part.Wire([hole_edge])))

    feat = doc.addObject("Part::Feature", "BracketShape")
    feat.Shape = bracket_face
    doc.recompute()

    # === TechDraw Page ===
    tmpl_path = os.path.join(FreeCAD.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg")  # noqa: PTH118
    page = doc.addObject("TechDraw::DrawPage", "Page")
    tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    tmpl.Template = tmpl_path
    page.Template = tmpl

    view = doc.addObject("TechDraw::DrawViewPart", "TopView")
    page.addView(view)
    view.Source = [feat]
    view.Direction = FreeCAD.Vector(0, 0, 1)
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

    # === Dimensions ===
    # All dimensions use TechDraw entity references (Edge/Vertex) so they update
    # automatically when the underlying sketch or spreadsheet parameters change.
    bb = feat.Shape.BoundBox
    cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2

    # Read solved positions from sketch geometry (used only for dimension label placement)
    hole_ctr = sk.Geometry[hole].Center
    solved_hole_r = sk.Geometry[hole].Radius
    solved_fillet_r = sk.Geometry[arc_tr].Radius
    arc_tr_ctr = sk.Geometry[arc_tr].Center
    tab_start_pt = sk.Geometry[tab_down].StartPoint
    tab_tip_pt = sk.Geometry[tab_down].EndPoint

    dim_off = 18

    # Identify TechDraw edges by geometric properties.
    # Edge indices vary between recomputes, so we match by shape characteristics.
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

    # Circular edges
    hole_edge_idx = find_edge(
        lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - solved_hole_r) < 0.1,
        f"circle R={solved_hole_r}",
    )
    # Two fillets have the same radius; pick the rightmost (top-right corner)
    fillet_matches = [
        (i, e)
        for i, e in enumerate(vis_edges)
        if isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - solved_fillet_r) < 0.1
    ]
    assert fillet_matches, f"No arc edge matching fillet radius {solved_fillet_r}"
    fillet_edge_idx = max(fillet_matches, key=lambda ie: ie[1].Curve.Center.x)[0]

    # Straight edges
    bot_left_edge_idx = find_edge(
        lambda e: isinstance(e.Curve, Part.Line) and _edge_dy(e) < 1 and e.Vertexes[0].Point.x < -cx + 1,
        "leftmost horizontal line",
    )
    tab_down_edge_idx = find_edge(
        lambda e: isinstance(e.Curve, Part.Line) and _edge_dx(e) > 1 and _edge_dy(e) > 1, "diagonal line (tab)"
    )
    # Vertical edges: left, right, tab_up — pick outermost pair for width measurement
    vert_line_edges = [
        (i, e)
        for i, e in enumerate(vis_edges)
        if isinstance(e.Curve, Part.Line) and _edge_dx(e) < 1 and _edge_dy(e) > 1
    ]
    left_edge_idx = min(vert_line_edges, key=lambda ie: ie[1].Vertexes[0].Point.x)[0]
    right_edge_idx = max(vert_line_edges, key=lambda ie: ie[1].Vertexes[0].Point.x)[0]
    # Horizontal edges — pick topmost for height measurement
    horiz_line_edges = [
        (i, e)
        for i, e in enumerate(vis_edges)
        if isinstance(e.Curve, Part.Line) and _edge_dy(e) < 1 and _edge_dx(e) > 1
    ]
    top_edge_idx = min(horiz_line_edges, key=lambda ie: ie[1].Vertexes[0].Point.y)[0]

    # 1. Overall width (left vertical edge to right vertical edge)
    d_w = doc.addObject("TechDraw::DrawViewDimension", "OverallWidth")
    page.addView(d_w)
    d_w.Type = "DistanceX"
    d_w.References2D = [(view, f"Edge{left_edge_idx}"), (view, f"Edge{right_edge_idx}")]
    d_w.X = 0
    d_w.Y = bb.YMin - dim_off - cy

    # 2. Overall height (bottom horizontal edge to top horizontal edge)
    d_h = doc.addObject("TechDraw::DrawViewDimension", "OverallHeight")
    page.addView(d_h)
    d_h.Type = "DistanceY"
    d_h.References2D = [(view, f"Edge{bot_left_edge_idx}"), (view, f"Edge{top_edge_idx}")]
    d_h.X = -dim_off - 10 - cx
    d_h.Y = 0

    # 3. Fillet radius
    d_fr = doc.addObject("TechDraw::DrawViewDimension", "FilletRadius")
    page.addView(d_fr)
    d_fr.Type = "Radius"
    d_fr.References2D = [(view, f"Edge{fillet_edge_idx}")]
    d_fr.X = arc_tr_ctr.x - cx + solved_fillet_r + 3
    d_fr.Y = arc_tr_ctr.y - cy + solved_fillet_r + 3

    # 4. Hole radius
    d_hr = doc.addObject("TechDraw::DrawViewDimension", "HoleRadius")
    page.addView(d_hr)
    d_hr.Type = "Radius"
    d_hr.References2D = [(view, f"Edge{hole_edge_idx}")]
    d_hr.X = hole_ctr.x - cx + solved_hole_r + 15
    d_hr.Y = hole_ctr.y - cy

    # 5. Tab length (single-edge distance = edge length)
    d_tl = doc.addObject("TechDraw::DrawViewDimension", "TabLength")
    page.addView(d_tl)
    d_tl.Type = "Distance"
    d_tl.References2D = [(view, f"Edge{tab_down_edge_idx}")]
    d_tl.X = (tab_start_pt.x + tab_tip_pt.x) / 2 - cx + 15
    d_tl.Y = (tab_start_pt.y + tab_tip_pt.y) / 2 - cy - 5

    # 6. Tab angle (between bottom horizontal edge and diagonal tab edge)
    d_angle = doc.addObject("TechDraw::DrawViewDimension", "TabAngle")
    page.addView(d_angle)
    d_angle.Type = "Angle"
    d_angle.References2D = [(view, f"Edge{bot_left_edge_idx}"), (view, f"Edge{tab_down_edge_idx}")]
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

    log("recompute after dimensions")
    doc.recompute(None, True, True)
    pump(qapp, 0.5)

    # === Save ===
    log("saving FCStd")
    fcstd_path = os.path.join(outdir, "bracket.FCStd")  # noqa: PTH118 — FreeCAD API expects str
    doc.saveAs(fcstd_path)
    log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes — done")


run_gui_script(qapp, _main)
