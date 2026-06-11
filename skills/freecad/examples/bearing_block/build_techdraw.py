"""
Create a multi-view TechDraw page for the bearing block FCStd.

Loads an existing bearing_block.FCStd and adds a TechDraw page with 4 views
(front, top, right, isometric) plus key dimensions. Saves the updated FCStd.
Use export_page.py to export to DXF/SVG/PDF.

Runs via the freecad GUI binary with Xvfb (needs Qt event pump for TechDraw
view computation). Reads INPUT env var for the FCStd path and OUTDIR for
output directory.

Usage:
  INPUT=/work/bearing_block.FCStd OUTDIR=/output \
    DISPLAY=:99 freecad build_bearing_block_techdraw.py
"""

import os
from pathlib import Path

import FreeCAD
import Part

from skills.freecad.examples.freecad_helpers import init_gui, log, pump, run_gui_script

input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")

qapp = init_gui()


def _main() -> None:
    # === Load document ===
    doc = FreeCAD.openDocument(input_path)
    FreeCAD.setActiveDocument(doc.Name)
    doc.recompute()

    # Find the Body (source for all views)
    body = doc.getObject("Body")
    assert body, "No Body object found in document"

    # Read parameters from spreadsheet for dimension placement
    sheet = doc.getObject("Params")
    base_l = float(sheet.get("B1"))
    base_w = float(sheet.get("B2"))
    base_h = float(sheet.get("B3"))
    boss_d = float(sheet.get("B4"))
    boss_h = float(sheet.get("B5"))
    bore_d = float(sheet.get("B6"))
    mount_d = float(sheet.get("B7"))
    mount_ix = float(sheet.get("B8"))
    mount_iy = float(sheet.get("B9"))

    total_h = base_h + boss_h

    # === TechDraw Page ===
    tmpl_path = os.path.join(  # noqa: PTH118
        FreeCAD.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg"
    )
    page = doc.addObject("TechDraw::DrawPage", "Page")
    tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    tmpl.Template = tmpl_path
    page.Template = tmpl

    # === Views ===
    # Layout on A4 Landscape (297 x 210 mm):
    #   Top-left: Front view    Top-right: Right view
    #   Bot-left: Top view      Bot-right: Isometric view
    scale = 0.8

    # Front view: looking along -Y (shows X-Z plane)
    front = doc.addObject("TechDraw::DrawViewPart", "FrontView")
    page.addView(front)
    front.Source = [body]
    front.Direction = FreeCAD.Vector(0, -1, 0)
    front.XDirection = FreeCAD.Vector(1, 0, 0)
    front.Scale = scale
    front.X = 90
    front.Y = 155

    # Right view: looking along +X (shows Y-Z plane)
    # GOTCHA: Axis-aligned directions can cause "failed to create projection CS"
    # errors in FreeCAD TechDraw. Fix by explicitly setting XDirection to resolve
    # the coordinate system ambiguity.
    right_v = doc.addObject("TechDraw::DrawViewPart", "RightView")
    page.addView(right_v)
    right_v.Source = [body]
    right_v.Direction = FreeCAD.Vector(1, 0, 0)
    right_v.XDirection = FreeCAD.Vector(0, 1, 0)
    right_v.Scale = scale
    right_v.X = 220
    right_v.Y = 155

    # Top view: looking along +Z (down at the boss side of the part)
    top_v = doc.addObject("TechDraw::DrawViewPart", "TopView")
    page.addView(top_v)
    top_v.Source = [body]
    top_v.Direction = FreeCAD.Vector(0, 0, 1)
    top_v.Scale = scale
    top_v.X = 90
    top_v.Y = 65

    # Isometric view
    iso = doc.addObject("TechDraw::DrawViewPart", "IsoView")
    page.addView(iso)
    iso.Source = [body]
    iso.Direction = FreeCAD.Vector(1, -1, 1)
    iso.Scale = scale * 0.7
    iso.X = 220
    iso.Y = 65

    doc.recompute(None, True, True)
    pump(qapp, 8)
    doc.recompute(None, True, True)
    pump(qapp, 3)

    # Verify views have edges
    for v_name in ("FrontView", "RightView", "TopView", "IsoView"):
        v = doc.getObject(v_name)
        n = len(v.getVisibleEdges())
        log(f"{v_name}: {n} visible edges")
        assert n > 0, f"{v_name} has 0 edges — Qt event pump may have failed"

    # === Dimensions ===
    #
    # All dimensions use References2D (projected edges/vertices). Edge indices vary
    # between recomputes, so match by geometric properties (length, position, type).

    def find_unique_edge(view, predicate, desc):
        """Find exactly one visible edge matching predicate. Asserts on 0 or 2+."""
        vis = view.getVisibleEdges()
        matches = [(i, e) for i, e in enumerate(vis) if predicate(e)]
        assert len(matches) == 1, (
            f"Expected 1 edge matching: {desc} (view {view.Name}), got {len(matches)}: "
            + ", ".join(
                f"Edge{i} ({type(e.Curve).__name__} dx={_edge_dx(e):.1f} dy={_edge_dy(e):.1f})" for i, e in matches
            )
        )
        return matches[0][0]

    def find_ranked_edge(view, predicate, key, desc):
        """Find the edge that maximizes `key` among all matches. Asserts if no matches."""
        vis = view.getVisibleEdges()
        matches = [(i, e) for i, e in enumerate(vis) if predicate(e)]
        assert matches, f"No edge matching: {desc} (view {view.Name} has {len(vis)} edges)"
        return max(matches, key=lambda ie: key(ie[1]))[0]

    def _edge_is_line(e):
        return isinstance(e.Curve, Part.Line)

    def _edge_dx(e):
        return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)

    def _edge_dy(e):
        return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)

    def add_dim(name, dim_type, refs2d, x, y, *, format_spec=None):
        d = doc.addObject("TechDraw::DrawViewDimension", name)
        page.addView(d)
        d.Type = dim_type
        d.References2D = refs2d
        d.X = x
        d.Y = y
        if format_spec is not None:
            d.FormatSpec = format_spec

    def add_ann(name, text, x, y, size=4):
        a = doc.addObject("TechDraw::DrawViewAnnotation", name)
        page.addView(a)
        a.Text = [text]
        a.X = x
        a.Y = y
        a.TextSize = size

    base_chamfer = float(sheet.get("B11"))
    boss_chamfer = float(sheet.get("B12"))

    # --- Front view dimensions ---

    # Log all front view edges for debugging
    front_vis = front.getVisibleEdges()
    for i, e in enumerate(front_vis):
        if len(e.Vertexes) >= 2:
            log(
                f"  FrontEdge{i}: {type(e.Curve).__name__} "
                f"dx={_edge_dx(e):.1f} dy={_edge_dy(e):.1f} "
                f"y0={e.Vertexes[0].Point.y:.1f} y1={e.Vertexes[1].Point.y:.1f}"
            )

    # Base bottom: full-width horizontal at highest Y (TechDraw Y inverted)
    bottom_idx = find_ranked_edge(
        front,
        lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > base_l * 0.9,
        lambda e: max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
        "front: base bottom (highest Y, wide horizontal)",
    )
    # Fillet arc: Circle edge with R near BossFilletRadius (projected, may differ)
    fillet_idx = find_ranked_edge(
        front,
        lambda e: isinstance(e.Curve, Part.Circle) and e.Curve.Radius > 1,
        lambda e: e.Curve.Radius,  # largest circle that isn't a mounting hole
        "front: fillet arc",
    )
    # Boss chamfer: diagonal line with dx≈dy≈BossChamfer (2mm)
    boss_chamfer_idx = find_ranked_edge(
        front,
        lambda e: _edge_is_line(e) and abs(_edge_dx(e) - boss_chamfer) < 0.5 and abs(_edge_dy(e) - boss_chamfer) < 0.5,
        lambda e: e.Vertexes[0].Point.x,  # rightmost chamfer line
        "front: boss chamfer line",
    )
    # Base chamfer: diagonal line with dx≈dy≈BaseChamfer (1mm)
    base_chamfer_idx = find_ranked_edge(
        front,
        lambda e: _edge_is_line(e) and abs(_edge_dx(e) - base_chamfer) < 0.5 and abs(_edge_dy(e) - base_chamfer) < 0.5,
        lambda e: e.Vertexes[0].Point.x,  # rightmost chamfer line
        "front: base chamfer line",
    )

    # ============================================================
    # Front view dimensions — overall envelope + chamfer/fillet
    # ============================================================

    # BaseLength — bottom edge
    add_dim("BaseLength", "DistanceX", [(front, f"Edge{bottom_idx}")], x=0, y=18)

    # TotalHeight — DistanceY between base bottom and boss top (chamfered) edges.
    # The boss top is a BSplineCurve with dx ≈ BossDiameter - 2*BossChamfer.
    boss_top_expected = boss_d - 2 * boss_chamfer
    boss_top_idx = find_unique_edge(
        front,
        lambda e: _edge_dy(e) < 0.5 and abs(_edge_dx(e) - boss_top_expected) < 1,
        f"front: boss top edge (dx≈{boss_top_expected})",
    )
    add_dim(
        "TotalHeight",
        "DistanceY",
        [(front, f"Edge{bottom_idx}"), (front, f"Edge{boss_top_idx}")],
        x=-base_l / 2 - 15,
        y=0,
    )

    # FilletRadius — fillet arc edge (left side, away from other dims)
    add_dim("FilletRadius", "Radius", [(front, f"Edge{fillet_idx}")], x=-boss_d / 2 - 12, y=3)

    # BossDiameter — measured on the front view where the boss profile is visible.
    # The boss projects as a BSplineCurve at the fillet junction with dx = BossDiameter.
    # Use DistanceX on this edge (measures projected X span = 40mm) with a ⌀ format.
    boss_outline_idx = find_ranked_edge(
        front,
        lambda e: not _edge_is_line(e) and _edge_dy(e) < 0.5 and abs(_edge_dx(e) - boss_d) < 2,
        lambda e: -e.Vertexes[0].Point.y,  # pick the one closest to boss top (most negative y)
        "front: boss outline BSpline dx≈40",
    )
    add_dim(
        "BossDiameter", "DistanceX", [(front, f"Edge{boss_outline_idx}")], x=0, y=-total_h - 10, format_spec="⌀%.0w"
    )

    # BossChamfer — DistanceX on chamfer line gives the horizontal leg (2mm).
    # FormatSpec "x45°" produces the standard engineering callout "2 x45°".
    add_dim(
        "BossChamferDim",
        "DistanceX",
        [(front, f"Edge{boss_chamfer_idx}")],
        x=boss_d / 2 + 10,
        y=-total_h + 2,
        format_spec="%.0f x45\u00b0",
    )

    # BaseChamfer — same DistanceX + "x45°" pattern
    add_dim(
        "BaseChamferDim",
        "DistanceX",
        [(front, f"Edge{base_chamfer_idx}")],
        x=base_l / 2 + 5,
        y=5,
        format_spec="%.0f x45\u00b0",
    )

    # ============================================================
    # Right view dimensions — BaseWidth + BaseHeight
    # ============================================================

    right_vis = right_v.getVisibleEdges()
    for i, e in enumerate(right_vis):
        if len(e.Vertexes) >= 2:
            log(f"  RightEdge{i}: {type(e.Curve).__name__} dx={_edge_dx(e):.1f} dy={_edge_dy(e):.1f}")

    # BaseWidth — longest horizontal edge in right view (base spans BaseWidth=60)
    right_width_idx = find_ranked_edge(
        right_v, lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5, _edge_dx, "right: longest horizontal (BaseWidth)"
    )
    add_dim("BaseWidthRight", "DistanceX", [(right_v, f"Edge{right_width_idx}")], x=0, y=18)

    # BaseHeight — two-edge DistanceY on right view (bottom and top-of-base horizontals)
    right_bottom_idx = find_ranked_edge(
        right_v,
        lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > base_w * 0.8,
        lambda e: max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
        "right: base bottom (highest Y, wide horizontal)",
    )
    right_top_idx = find_ranked_edge(
        right_v,
        lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > base_w * 0.8,
        lambda e: -max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
        "right: base top (min Y, wide horizontal)",
    )
    add_dim(
        "BaseHeightRight",
        "DistanceY",
        [(right_v, f"Edge{right_bottom_idx}"), (right_v, f"Edge{right_top_idx}")],
        x=base_w / 2 + 15,
        y=5,
    )

    # ============================================================
    # Top view dimensions — bore, boss diameter, mounting holes
    # ============================================================

    top_vis = top_v.getVisibleEdges()
    for i, e in enumerate(top_vis):
        if len(e.Vertexes) >= 2:
            extra = ""
            if isinstance(e.Curve, Part.Circle):
                extra = f" R={e.Curve.Radius:.1f}"
            log(f"  TopEdge{i}: {type(e.Curve).__name__} dx={_edge_dx(e):.1f} dy={_edge_dy(e):.1f}{extra}")

    # Bore diameter — circle edge
    bore_r = bore_d / 2
    bore_edge_idx = find_unique_edge(
        top_v,
        lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - bore_r) < 1.0,
        f"top: bore circle R={bore_r}",
    )
    add_dim("BoreDiameter", "Diameter", [(top_v, f"Edge{bore_edge_idx}")], x=bore_r + 18, y=5)

    # Mounting hole diameter — pick a corner hole, place label horizontally to the right
    mount_r = mount_d / 2
    mount_hole_idx = find_ranked_edge(
        top_v,
        lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - mount_r) < 1.0,
        lambda e: e.Curve.Center.x - e.Curve.Center.y,
        "top: corner mounting hole circle",
    )
    # For Diameter dims, dim.X/dim.Y are offsets from the circle center.
    # Y=0 gives a horizontal leader line.
    add_dim("MountHoleDiameter", "Diameter", [(top_v, f"Edge{mount_hole_idx}")], x=base_l / 2 + 20, y=0)

    # Mounting hole inset dimensions via vertex references.
    # Each circle in TechDraw generates 3 vertices: 2 at the perimeter start/end
    # and 1 at the center. We find the center vertex by matching coordinates.

    def find_vertex(view, predicate, desc):
        """Find a TechDraw vertex by geometric predicate. Returns 'VertexN' string."""
        n_verts = len(view.getVisibleVertexes())
        matches = []
        for vi in range(n_verts):
            v = view.getVertexBySelection(f"Vertex{vi}")
            if predicate(v.Point):
                matches.append((vi, v.Point))
        assert len(matches) >= 1, f"No vertex matching: {desc} ({n_verts} total)"
        return f"Vertex{matches[0][0]}"

    def find_circle_center_vertex(view, circle_edge_idx):
        """Find the TechDraw vertex at the center of a circle edge."""
        circle = view.getVisibleEdges()[circle_edge_idx]
        cx, cy = circle.Curve.Center.x, circle.Curve.Center.y
        return find_vertex(
            view,
            lambda pt: abs(pt.x - cx) < 0.5 and abs(pt.y - cy) < 0.5,
            f"center of circle Edge{circle_edge_idx} at ({cx:.1f}, {cy:.1f})",
        )

    # Pick the top-left hole for inset dims — emptiest quadrant, away from
    # ⌀8/⌀20/⌀40 dims which cluster around center and right side.
    # TechDraw Y is inverted: top in rendered view = most negative y.
    inset_hole_idx = find_ranked_edge(
        top_v,
        lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - mount_r) < 1.0,
        lambda e: -e.Curve.Center.x - e.Curve.Center.y,
        "top: top-left mounting hole circle",
    )
    inset_hole_vert = find_circle_center_vertex(top_v, inset_hole_idx)
    log(f"Inset hole: Edge{inset_hole_idx}, center vertex: {inset_hole_vert}")

    # Left plate edge (vertical, most negative x)
    top_left_edge_idx = find_ranked_edge(
        top_v,
        lambda e: _edge_is_line(e) and _edge_dx(e) < 1 and _edge_dy(e) > base_w * 0.8,
        lambda e: -max(e.Vertexes[0].Point.x, e.Vertexes[1].Point.x),
        "top: left plate edge",
    )

    # MountHoleInsetX: hole center to left plate edge
    add_dim(
        "MountInsetX",
        "DistanceX",
        [(top_v, inset_hole_vert), (top_v, f"Edge{top_left_edge_idx}")],
        x=-base_l / 2 + mount_ix / 2,
        y=-base_w / 2 - 16,
    )

    # MountHoleInsetY: hole center to nearest corner vertex.
    # DistanceY between a vertex and a full-width horizontal edge measures to
    # the far endpoint, not the perpendicular projection. Use two vertices
    # (hole center + corner on the same edge) for correct perpendicular distance.
    top_left_corner = find_vertex(
        top_v, lambda pt: abs(pt.x - (-base_l / 2)) < 1 and abs(pt.y - (-base_w / 2)) < 1, "top-left plate corner"
    )
    add_dim(
        "MountInsetY",
        "DistanceY",
        [(top_v, inset_hole_vert), (top_v, top_left_corner)],
        x=-base_l / 2 - 16,
        y=-base_w / 2 + mount_iy / 2,
    )

    # === Centerlines on top view ===
    # Boss/bore centerline cross: cosmetic dash-dot lines through the bore center,
    # extending slightly beyond the boss diameter.
    cl_extent = boss_d / 2 + 8
    top_v.makeCosmeticLine(FreeCAD.Vector(0, -cl_extent, 0), FreeCAD.Vector(0, cl_extent, 0), 2)
    top_v.makeCosmeticLine(FreeCAD.Vector(-cl_extent, 0, 0), FreeCAD.Vector(cl_extent, 0, 0), 2)

    # Center marks on all 4 mounting holes (small cross at each hole center)
    mark_size = mount_r + 2
    top_vis = top_v.getVisibleEdges()
    for e in top_vis:
        if isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - mount_r) < 1.0:
            cx, cy = e.Curve.Center.x, e.Curve.Center.y
            top_v.makeCosmeticLine(FreeCAD.Vector(cx, cy - mark_size, 0), FreeCAD.Vector(cx, cy + mark_size, 0), 2)
            top_v.makeCosmeticLine(FreeCAD.Vector(cx - mark_size, cy, 0), FreeCAD.Vector(cx + mark_size, cy, 0), 2)

    # === Annotations — positioned in title block area (bottom-right) ===
    add_ann("Title", "Flanged Bearing Block", x=250, y=15, size=6)
    add_ann("Material", "Material: Aluminium 6061", x=250, y=22)

    doc.recompute(None, True, True)
    pump(qapp, 2)

    # === Save ===
    fcstd_path = os.path.join(outdir, "bearing_block.FCStd")  # noqa: PTH118
    doc.saveAs(fcstd_path)
    log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes — done")


run_gui_script(qapp, _main)
