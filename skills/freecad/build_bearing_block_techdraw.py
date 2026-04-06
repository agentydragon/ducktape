"""
Create a multi-view TechDraw page for the bearing block FCStd.

Loads an existing bearing_block.FCStd and adds a TechDraw page with 4 views
(front, top, right, isometric) plus key dimensions. Saves the updated FCStd.
Use export_page.py to export to DXF/SVG/PDF.

Runs inside freecadcmd under xvfb (needs Qt event pump for TechDraw view computation).
Reads INPUT env var for the FCStd path and OUTDIR for output directory.

Usage:
  INPUT=/work/bearing_block.FCStd OUTDIR=/output \
    xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd build_bearing_block_techdraw.py
"""

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import FreeCAD as App
import FreeCADGui as Gui
import Part
from freecad_helpers import init_gui, log, pump

Gui.showMainWindow()
qapp = init_gui()

input_path = os.environ.get("INPUT", "bearing_block.FCStd")
outdir = os.environ.get("OUTDIR", ".")


# === Load document ===
doc = App.openDocument(input_path)
App.setActiveDocument(doc.Name)
doc.recompute()

# Find the Body (source for all views)
body = doc.getObject("Body")
assert body, "No Body object found in document"

# Read parameters from spreadsheet for dimension placement
sheet = doc.getObject("Params")
BASE_L = float(sheet.get("B1"))
BASE_W = float(sheet.get("B2"))
BASE_H = float(sheet.get("B3"))
BOSS_D = float(sheet.get("B4"))
BOSS_H = float(sheet.get("B5"))
BORE_D = float(sheet.get("B6"))
MOUNT_D = float(sheet.get("B7"))
MOUNT_IX = float(sheet.get("B8"))
MOUNT_IY = float(sheet.get("B9"))

TOTAL_H = BASE_H + BOSS_H

# === TechDraw Page ===
tmpl_path = os.path.join(  # noqa: PTH118
    App.getResourceDir(), "Mod", "TechDraw", "Templates", "ISO", "A4_Landscape_blank.svg"
)
page = doc.addObject("TechDraw::DrawPage", "Page")
tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
tmpl.Template = tmpl_path
page.Template = tmpl

# === Views ===
# Layout on A4 Landscape (297 x 210 mm):
#   Top-left: Front view    Top-right: Right view
#   Bot-left: Top view      Bot-right: Isometric view
SCALE = 0.8

# Front view: looking along -Y (shows X-Z plane)
front = doc.addObject("TechDraw::DrawViewPart", "FrontView")
page.addView(front)
front.Source = [body]
front.Direction = App.Vector(0, -1, 0)
front.XDirection = App.Vector(1, 0, 0)
front.Scale = SCALE
front.X = 90
front.Y = 155

# Right view: looking along +X (shows Y-Z plane)
# GOTCHA: Axis-aligned directions can cause "failed to create projection CS"
# errors in FreeCAD TechDraw. Fix by explicitly setting XDirection to resolve
# the coordinate system ambiguity.
right_v = doc.addObject("TechDraw::DrawViewPart", "RightView")
page.addView(right_v)
right_v.Source = [body]
right_v.Direction = App.Vector(1, 0, 0)
right_v.XDirection = App.Vector(0, 1, 0)
right_v.Scale = SCALE
right_v.X = 220
right_v.Y = 155

# Top view: looking along -Z (shows X-Y plane)
top_v = doc.addObject("TechDraw::DrawViewPart", "TopView")
page.addView(top_v)
top_v.Source = [body]
top_v.Direction = App.Vector(0, 0, -1)
top_v.Scale = SCALE
top_v.X = 90
top_v.Y = 65

# Isometric view
iso = doc.addObject("TechDraw::DrawViewPart", "IsoView")
page.addView(iso)
iso.Source = [body]
iso.Direction = App.Vector(1, -1, 1)
iso.Scale = SCALE * 0.7
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


# Install excepthook that calls os._exit(1) after printing the traceback.
# Without this, unhandled exceptions let Python unwind normally, FreeCAD's
# atexit runs, and Qt segfaults in QOpenGLContext::currentContext() —
# overwriting the useful error message with a useless native stack trace.
def _excepthook(exc_type, exc_value, exc_tb):
    traceback.print_exception(exc_type, exc_value, exc_tb)
    os._exit(1)


sys.excepthook = _excepthook


# === Dimensions ===
#
# Two approaches are used:
# 1. References3D + MeasureType="True" — for cylindrical faces (diameter/radius).
#    TechDraw measures the 3D geometry directly, independent of projection.
# 2. References2D (projected edges) — for linear distances, chamfers, fillets.
#    Edge indices vary between recomputes, so match by geometric properties.
#
# TODO: Face-to-face distance (e.g., BaseHeight as distance between base bottom
# and base top faces) is supported by FreeCAD's Measurement engine
# (planePlaneDistance) but NOT wired into DrawViewDimension.getTrueDimValue()
# as of FreeCAD 1.1.0. When this gap is fixed upstream, convert the height
# dimensions from projected-edge DistanceY to References3D face pairs.


def find_unique_edge(view, predicate, desc):
    """Find exactly one visible edge matching predicate. Asserts on 0 or 2+."""
    vis = view.getVisibleEdges()
    matches = [(i, e) for i, e in enumerate(vis) if predicate(e)]
    assert len(matches) == 1, f"Expected 1 edge matching: {desc} (view {view.Name}), got {len(matches)}: " + ", ".join(
        f"Edge{i} ({type(e.Curve).__name__} dx={_edge_dx(e):.1f} dy={_edge_dy(e):.1f})" for i, e in matches
    )
    return matches[0][0]


def find_ranked_edge(view, predicate, key, desc):
    """Find the edge that maximizes `key` among all matches. Asserts if no matches."""
    vis = view.getVisibleEdges()
    matches = [(i, e) for i, e in enumerate(vis) if predicate(e)]
    assert matches, f"No edge matching: {desc} (view {view.Name} has {len(vis)} edges)"
    return max(matches, key=lambda ie: key(ie[1]))[0]


def find_3d_face(shape, predicate, desc):
    """Find a 3D face by geometric properties. Returns 'FaceN' string."""
    matches = [(i, f) for i, f in enumerate(shape.Faces, 1) if predicate(f)]
    assert len(matches) == 1, f"Expected 1 face matching: {desc}, got {len(matches)}: " + ", ".join(
        f"Face{i} ({type(f.Surface).__name__})" for i, f in matches
    )
    return f"Face{matches[0][0]}"


def _edge_is_line(e):
    return isinstance(e.Curve, Part.Line)


def _edge_dx(e):
    return abs(e.Vertexes[1].Point.x - e.Vertexes[0].Point.x)


def _edge_dy(e):
    return abs(e.Vertexes[1].Point.y - e.Vertexes[0].Point.y)


tip_shape = body.Tip.Shape
BOSS_FILLET_R = float(sheet.get("B10"))
BASE_CHAMFER = float(sheet.get("B11"))
BOSS_CHAMFER = float(sheet.get("B12"))

# --- 3D face identification for References3D dimensions ---
# Boss cylinder: Cylinder face with R = BossDiameter/2
boss_cyl_face = find_3d_face(
    tip_shape,
    lambda f: type(f.Surface).__name__ == "Cylinder" and abs(f.Surface.Radius - BOSS_D / 2) < 0.5,
    f"boss cylinder R={BOSS_D / 2}",
)
# Bore cylinder: Cylinder face with R = BoreDiameter/2
bore_cyl_face = find_3d_face(
    tip_shape,
    lambda f: type(f.Surface).__name__ == "Cylinder" and abs(f.Surface.Radius - BORE_D / 2) < 0.5,
    f"bore cylinder R={BORE_D / 2}",
)
# Mounting hole: any Cylinder face with R = MountHoleDiameter/2 (pick first by lowest x)
mount_hole_faces = [
    (i, f)
    for i, f in enumerate(tip_shape.Faces, 1)
    if type(f.Surface).__name__ == "Cylinder" and abs(f.Surface.Radius - MOUNT_D / 2) < 0.5
]
assert mount_hole_faces, f"No mounting hole cylinder face R={MOUNT_D / 2}"
mount_hole_face = f"Face{min(mount_hole_faces, key=lambda x: x[1].CenterOfMass.x)[0]}"

log(f"3D faces: boss={boss_cyl_face}, bore={bore_cyl_face}, hole={mount_hole_face}")

# --- Front view dimensions (References2D, projected edges) ---

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
    lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > BASE_L * 0.9,
    lambda e: max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
    "front: base bottom (highest Y, wide horizontal)",
)
# Base top: wide horizontal at SECOND-highest Y (may be chamfered, dx ≈ BaseLength - 2*BaseChamfer)
base_top_idx = find_ranked_edge(
    front,
    lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > BASE_L * 0.9,
    lambda e: -max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),  # min Y among wide = base top
    "front: base top (min Y, wide horizontal)",
)
# Left base vertical: leftmost vertical line (spans BaseHeight minus chamfer)
left_vert_idx = find_ranked_edge(
    front,
    lambda e: _edge_is_line(e) and _edge_dx(e) < 0.5 and _edge_dy(e) > 1,
    lambda e: -e.Vertexes[0].Point.x,  # leftmost
    "front: left base vertical",
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
    lambda e: _edge_is_line(e) and abs(_edge_dx(e) - BOSS_CHAMFER) < 0.5 and abs(_edge_dy(e) - BOSS_CHAMFER) < 0.5,
    lambda e: e.Vertexes[0].Point.x,  # rightmost chamfer line
    "front: boss chamfer line",
)
# Base chamfer: diagonal line with dx≈dy≈BaseChamfer (1mm)
base_chamfer_idx = find_ranked_edge(
    front,
    lambda e: _edge_is_line(e) and abs(_edge_dx(e) - BASE_CHAMFER) < 0.5 and abs(_edge_dy(e) - BASE_CHAMFER) < 0.5,
    lambda e: e.Vertexes[0].Point.x,  # rightmost chamfer line
    "front: base chamfer line",
)

# ============================================================
# Front view dimensions — overall envelope + chamfer/fillet
# ============================================================

# BaseLength — bottom edge
d = doc.addObject("TechDraw::DrawViewDimension", "BaseLength")
page.addView(d)
d.Type = "DistanceX"
d.References2D = [(front, f"Edge{bottom_idx}")]
d.X = 0
d.Y = 18

# TotalHeight — DistanceY between base bottom and boss top (chamfered) edges.
# The boss top is a BSplineCurve with dx ≈ BossDiameter - 2*BossChamfer.
boss_top_expected = BOSS_D - 2 * BOSS_CHAMFER
boss_top_idx = find_unique_edge(
    front,
    lambda e: _edge_dy(e) < 0.5 and abs(_edge_dx(e) - boss_top_expected) < 1,
    f"front: boss top edge (dx≈{boss_top_expected})",
)
d = doc.addObject("TechDraw::DrawViewDimension", "TotalHeight")
page.addView(d)
d.Type = "DistanceY"
d.References2D = [(front, f"Edge{bottom_idx}"), (front, f"Edge{boss_top_idx}")]
d.X = -BASE_L / 2 - 15
d.Y = 0

# FilletRadius — fillet arc edge (left side, away from other dims)
d = doc.addObject("TechDraw::DrawViewDimension", "FilletRadius")
page.addView(d)
d.Type = "Radius"
d.References2D = [(front, f"Edge{fillet_idx}")]
d.X = -BOSS_D / 2 - 12
d.Y = 3

# BossChamfer — DistanceX on chamfer line gives the horizontal leg (2mm).
# FormatSpec "x45°" produces the standard engineering callout "2 x45°".
d = doc.addObject("TechDraw::DrawViewDimension", "BossChamferDim")
page.addView(d)
d.Type = "DistanceX"
d.References2D = [(front, f"Edge{boss_chamfer_idx}")]
d.FormatSpec = "%.0f x45\u00b0"
d.X = BOSS_D / 2 + 10
d.Y = -TOTAL_H + 2

# BaseChamfer — same DistanceX + "x45°" pattern
d = doc.addObject("TechDraw::DrawViewDimension", "BaseChamferDim")
page.addView(d)
d.Type = "DistanceX"
d.References2D = [(front, f"Edge{base_chamfer_idx}")]
d.FormatSpec = "%.0f x45\u00b0"
d.X = BASE_L / 2 + 5
d.Y = 5

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
d = doc.addObject("TechDraw::DrawViewDimension", "BaseWidthRight")
page.addView(d)
d.Type = "DistanceX"
d.References2D = [(right_v, f"Edge{right_width_idx}")]
d.X = 0
d.Y = 18

# BaseHeight — two-edge DistanceY on right view (bottom and top-of-base horizontals)
right_bottom_idx = find_ranked_edge(
    right_v,
    lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > BASE_W * 0.8,
    lambda e: max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
    "right: base bottom (highest Y, wide horizontal)",
)
right_top_idx = find_ranked_edge(
    right_v,
    lambda e: _edge_is_line(e) and _edge_dy(e) < 0.5 and _edge_dx(e) > BASE_W * 0.8,
    lambda e: -max(e.Vertexes[0].Point.y, e.Vertexes[1].Point.y),
    "right: base top (min Y, wide horizontal)",
)
d = doc.addObject("TechDraw::DrawViewDimension", "BaseHeightRight")
page.addView(d)
d.Type = "DistanceY"
d.References2D = [(right_v, f"Edge{right_bottom_idx}"), (right_v, f"Edge{right_top_idx}")]
d.X = BASE_W / 2 + 15
d.Y = 5

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
bore_r = BORE_D / 2
bore_edge_idx = find_unique_edge(
    top_v,
    lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - bore_r) < 1.0,
    f"top: bore circle R={bore_r}",
)
d = doc.addObject("TechDraw::DrawViewDimension", "BoreDiameter")
page.addView(d)
d.Type = "Diameter"
d.References2D = [(top_v, f"Edge{bore_edge_idx}")]
d.X = bore_r + 18
d.Y = 5

# Boss diameter — 3D cylindrical face, placed on top view.
# The boss does NOT project as a R=20 circle in the top view because the fillet
# smooths the base junction and the chamfer shrinks the top face. Use References3D
# with the boss cylindrical face (R=20) for the correct ⌀40 measurement.
d = doc.addObject("TechDraw::DrawViewDimension", "BossDiameter")
page.addView(d)
d.Type = "Diameter"
d.MeasureType = "True"
d.References2D = [(top_v, "")]
d.References3D = [(body.Tip, boss_cyl_face)]
d.X = -BOSS_D / 2 - 15
d.Y = -5

# Mounting hole diameter — pick the top-right hole circle (positive x, negative y in TechDraw)
mount_r = MOUNT_D / 2
mount_hole_idx = find_ranked_edge(
    top_v,
    lambda e: isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - mount_r) < 1.0,
    lambda e: e.Curve.Center.x - e.Curve.Center.y,  # top-right in TechDraw coords
    "top: top-right mounting hole circle",
)
d = doc.addObject("TechDraw::DrawViewDimension", "MountHoleDiameter")
page.addView(d)
d.Type = "Diameter"
d.References2D = [(top_v, f"Edge{mount_hole_idx}")]
d.X = BASE_L / 2 - MOUNT_IX + 12
d.Y = -BASE_W / 2 + MOUNT_IY - 8

# TODO: Mounting hole inset dimensions (MountHoleInsetX=15, MountHoleInsetY=15).
# Two-edge DistanceX/Y between a Line and a Circle produces incorrect values.
# Needs vertex references or LandmarkDimension.

# === Annotations — positioned in title block area (bottom-right) ===
ann_title = doc.addObject("TechDraw::DrawViewAnnotation", "Title")
page.addView(ann_title)
ann_title.Text = ["Flanged Bearing Block"]
ann_title.X = 250
ann_title.Y = 15
ann_title.TextSize = 6

ann_material = doc.addObject("TechDraw::DrawViewAnnotation", "Material")
page.addView(ann_material)
ann_material.Text = ["Material: Aluminium 6061"]
ann_material.X = 250
ann_material.Y = 22
ann_material.TextSize = 4

doc.recompute(None, True, True)
pump(qapp, 2)

# === Save ===
fcstd_path = os.path.join(outdir, "bearing_block.FCStd")  # noqa: PTH118
doc.saveAs(fcstd_path)
log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes")

# MUST use os._exit to skip Qt cleanup which segfaults in headless mode.
# Without this wrapper, an assertion failure lets Python unwind normally,
# FreeCAD's atexit runs, and Qt segfaults in QOpenGLContext::currentContext(),
# which overwrites the useful error message with a useless stack trace.
os._exit(0)
