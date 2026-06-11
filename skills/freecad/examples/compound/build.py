"""
Build a compound shape from a wall shell and a closed rectangle, then export via TechDraw.

Demonstrates:
- Spreadsheet-driven parameters with aliases and setExpression() bindings
- Part.makeCompound for grouping multiple faces into a single Part::Feature
- Wall shell as fully constrained sketch geometry (inner + outer outlines with thickness)
- Entity-referenced TechDraw dimensions that auto-update when parameters change
- Single compound, single TechDraw view (preserves relative positions)

Runs under the FreeCAD GUI binary with Xvfb.
Output directory is read from OUTDIR env var (default: current directory).

Usage:
  Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
  DISPLAY=:99 OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecad build_compound.py
"""

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
    doc = FreeCAD.newDocument("CompoundExample")

    # === Spreadsheet Parameters ===
    sheet = doc.addObject("Spreadsheet::Sheet", "Params")

    params = [
        (1, "RoomWidth", "4000"),
        (2, "RoomHeight", "3000"),
        (3, "TableWidth", "1200"),
        (4, "TableHeight", "600"),
        (5, "TableX", "500"),
        (6, "TableY", "500"),
        (7, "WallThickness", "150"),
    ]
    for row, label, value in params:
        sheet.set(f"A{row}", label)
        sheet.set(f"B{row}", value)
        sheet.setAlias(f"B{row}", label)

    # Computed intermediates
    computed = [
        (9, "RoomWidthPlusThickness", "=RoomWidth + WallThickness"),
        (10, "RoomHeightPlus2Thickness", "=RoomHeight + 2 * WallThickness"),
        (11, "RoomWidthMinusThickness", "=RoomWidth - WallThickness"),
    ]
    for row, label, formula in computed:
        sheet.set(f"A{row}", label)
        sheet.set(f"B{row}", formula)
        sheet.setAlias(f"B{row}", label)

    doc.recompute()

    # Read spreadsheet values for initial geometry placement
    room_w = float(sheet.get("B1"))
    room_h = float(sheet.get("B2"))
    table_w = float(sheet.get("B3"))
    table_h = float(sheet.get("B4"))
    table_x = float(sheet.get("B5"))
    table_y = float(sheet.get("B6"))
    t = float(sheet.get("B7"))

    log(f"Params: {room_w=}, {room_h=}, {table_w=}, {table_h=}, {table_x=}, {table_y=}, {t=}")

    # === Sketch (fully constrained) ===
    sk = doc.addObject("Sketcher::SketchObject", "Layout")

    # Wall shell: L-shaped closed polygon with 6 vertices tracing inner and outer outlines.
    # All geometry is constrained — dimensions drive the shape.
    #
    #    p4(Rw-t,Rh+t)──p3(Rw+t,Rh+t)
    #         │              │
    #         │  right wall  │
    #         │              │
    #    p5(Rw-t,t)          │
    #         │              │
    #  p6(0,t)┘              │
    #    │                   │
    #  p1(0,-t)──────────p2(Rw+t,-t)
    #        bottom wall
    #
    p1 = FreeCAD.Vector(0, -t, 0)  # bottom-left outer
    p2 = FreeCAD.Vector(room_w + t, -t, 0)  # bottom-right outer
    p3 = FreeCAD.Vector(room_w + t, room_h + t, 0)  # top-right outer
    p4 = FreeCAD.Vector(room_w - t, room_h + t, 0)  # top-right inner (cap)
    p5 = FreeCAD.Vector(room_w - t, t, 0)  # inner L-bend
    p6 = FreeCAD.Vector(0, t, 0)  # bottom-left inner

    s1 = sk.addGeometry(Part.LineSegment(p1, p2))  # bottom outer
    s2 = sk.addGeometry(Part.LineSegment(p2, p3))  # right outer
    s3 = sk.addGeometry(Part.LineSegment(p3, p4))  # top cap
    s4 = sk.addGeometry(Part.LineSegment(p4, p5))  # right inner
    s5 = sk.addGeometry(Part.LineSegment(p5, p6))  # inner horizontal
    s6 = sk.addGeometry(Part.LineSegment(p6, p1))  # left cap
    wall_indices = [s1, s2, s3, s4, s5, s6]

    # Chain corners (each segment end → next segment start)
    for i in range(6):
        sk.addConstraint(Sketcher.Constraint("Coincident", wall_indices[i], 2, wall_indices[(i + 1) % 6], 1))

    # Orientation constraints
    for i in [s1, s3, s5]:  # bottom outer, top cap, inner horizontal
        sk.addConstraint(Sketcher.Constraint("Horizontal", i))
    for i in [s2, s4, s6]:  # right outer, right inner, left cap
        sk.addConstraint(Sketcher.Constraint("Vertical", i))

    # Pin bottom-left outer corner (s1 start) at origin X, below origin Y by thickness
    sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, s1, 1, 0.0))
    c_piny = sk.addConstraint(Sketcher.Constraint("DistanceY", s1, 1, -1, 1, t))
    sk.setExpression(f"Constraints[{c_piny}]", "Params.WallThickness")

    # Dimensional constraints bound to spreadsheet
    dim_bindings = [
        (Sketcher.Constraint("DistanceX", s1, 1, s1, 2, room_w + t), "Params.RoomWidthPlusThickness"),
        (Sketcher.Constraint("DistanceY", s2, 1, s2, 2, room_h + 2 * t), "Params.RoomHeightPlus2Thickness"),
        (Sketcher.Constraint("DistanceY", s4, 2, s4, 1, room_h), "Params.RoomHeight"),
        (Sketcher.Constraint("DistanceX", s5, 2, s5, 1, room_w - t), "Params.RoomWidthMinusThickness"),
    ]
    for constraint, expr in dim_bindings:
        idx = sk.addConstraint(constraint)
        sk.setExpression(f"Constraints[{idx}]", expr)

    # Table: fully constrained rectangle
    x, y, tw, th = table_x, table_y, table_w, table_h
    t0 = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(x, y, 0), FreeCAD.Vector(x + tw, y, 0)))
    t1 = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(x + tw, y, 0), FreeCAD.Vector(x + tw, y + th, 0)))
    t2 = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(x + tw, y + th, 0), FreeCAD.Vector(x, y + th, 0)))
    t3 = sk.addGeometry(Part.LineSegment(FreeCAD.Vector(x, y + th, 0), FreeCAD.Vector(x, y, 0)))
    for a, b in [(t0, t1), (t1, t2), (t2, t3), (t3, t0)]:
        sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))
    for i in [t0, t2]:
        sk.addConstraint(Sketcher.Constraint("Horizontal", i))
    for i in [t1, t3]:
        sk.addConstraint(Sketcher.Constraint("Vertical", i))

    table_bindings = [
        (Sketcher.Constraint("DistanceX", t0, 1, t0, 2, tw), "Params.TableWidth"),
        (Sketcher.Constraint("DistanceY", t1, 1, t1, 2, th), "Params.TableHeight"),
        (Sketcher.Constraint("DistanceX", -1, 1, t0, 1, x), "Params.TableX"),
        (Sketcher.Constraint("DistanceY", -1, 1, t0, 1, y), "Params.TableY"),
    ]
    for constraint, expr in table_bindings:
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
                FreeCAD.Vector(sk.Geometry[i].StartPoint.x, sk.Geometry[i].StartPoint.y, 0),
                FreeCAD.Vector(sk.Geometry[i].EndPoint.x, sk.Geometry[i].EndPoint.y, 0),
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

    # === Dimensions (entity-referenced) ===
    bb = feat.Shape.BoundBox
    cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2
    scale = float(view.Scale)

    # Read solved geometry for label placement
    room_w_solved = float(sheet.get("B1"))
    table_w_solved = float(sheet.get("B3"))
    table_h_solved = float(sheet.get("B4"))
    wall_t_solved = float(sheet.get("B7"))

    dim_off = 0.8  # view-local offset for dimension lines (in view-scaled mm)

    # Identify TechDraw edges by geometric properties.
    vis_edges = view.getVisibleEdges()

    def _edge_dx(e):
        return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)

    def _edge_dy(e):
        return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)

    def _edge_midx(e):
        return (e.Vertexes[0].Point.x + e.Vertexes[1].Point.x) / 2

    def _edge_midy(e):
        return (e.Vertexes[0].Point.y + e.Vertexes[1].Point.y) / 2

    def _edge_miny(e):
        return min(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y)

    def _edge_minx(e):
        return min(e.Vertexes[0].Point.x, e.Vertexes[1].Point.x)

    # Edge lengths in view-local coords (sketch mm * scale)
    table_bot_len = table_w_solved * scale
    table_right_len = table_h_solved * scale

    # Bottom outer wall (longest horizontal at bottom of view)
    horiz_edges = [
        (i, e)
        for i, e in enumerate(vis_edges)
        if isinstance(e.Curve, Part.Line) and _edge_dy(e) < 0.1 and _edge_dx(e) > 1
    ]
    bottom_outer_idx = min(horiz_edges, key=lambda ie: _edge_midy(ie[1]))[0]

    # Right outer wall (longest vertical at right of view)
    vert_edges = [
        (i, e)
        for i, e in enumerate(vis_edges)
        if isinstance(e.Curve, Part.Line) and _edge_dx(e) < 0.1 and _edge_dy(e) > 1
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

    # 1. Room width (bottom outer edge)
    d_w = doc.addObject("TechDraw::DrawViewDimension", "RoomWidth")
    page.addView(d_w)
    d_w.Type = "DistanceX"
    d_w.References2D = [(view, f"Edge{bottom_outer_idx}")]
    d_w.X = 0
    d_w.Y = (bb.YMin - cy) * scale - dim_off

    # 2. Room height (right outer edge)
    d_h = doc.addObject("TechDraw::DrawViewDimension", "RoomHeight")
    page.addView(d_h)
    d_h.Type = "DistanceY"
    d_h.References2D = [(view, f"Edge{right_outer_idx}")]
    d_h.X = (bb.XMax - cx) * scale + dim_off + 0.5
    d_h.Y = 0

    # 3. Table width (table bottom edge)
    d_tw = doc.addObject("TechDraw::DrawViewDimension", "TableWidth")
    page.addView(d_tw)
    d_tw.Type = "DistanceX"
    d_tw.References2D = [(view, f"Edge{table_bot_idx}")]
    d_tw.X = _edge_midx(vis_edges[table_bot_idx])
    d_tw.Y = _edge_midy(vis_edges[table_bot_idx]) - dim_off * 0.6

    # 4. Table height (table right edge)
    d_th = doc.addObject("TechDraw::DrawViewDimension", "TableHeight")
    page.addView(d_th)
    d_th.Type = "DistanceY"
    d_th.References2D = [(view, f"Edge{table_right_idx}")]
    d_th.X = _edge_midx(vis_edges[table_right_idx]) + dim_off * 0.6
    d_th.Y = _edge_midy(vis_edges[table_right_idx])

    # 5. Wall thickness (between bottom outer and inner horizontal edges)
    if inner_horiz_idx is not None:
        d_wt = doc.addObject("TechDraw::DrawViewDimension", "WallThickness")
        page.addView(d_wt)
        d_wt.Type = "DistanceY"
        d_wt.References2D = [(view, f"Edge{bottom_outer_idx}"), (view, f"Edge{inner_horiz_idx}")]
        d_wt.X = (bb.XMin - cx) * scale - dim_off
        d_wt.Y = ((bb.YMin + wall_t_solved) - cy) * scale

    log("recompute after dimensions")
    doc.recompute(None, True, True)
    pump(qapp, 0.5)

    # === Save ===
    log("saving FCStd")
    out_path = os.path.join(outdir, "compound.FCStd")  # noqa: PTH118 — FreeCAD API expects str
    doc.saveAs(out_path)
    log(f"FCStd: {Path(out_path).stat().st_size} bytes — done")

    # suppress unused warnings for helpers only called indirectly via local scope


run_gui_script(qapp, _main)
