"""
Parametric flanged bearing block using Part Design workbench.

Demonstrates: PartDesign::Body, Pad, Pocket (through), Fillet, Chamfer,
sketching on XY plane and on faces, spreadsheet-driven parameters with
expressions binding feature lengths and sketch constraints.

Runs inside freecadcmd (no GUI needed for modeling).
Output directory is read from OUTDIR env var (default: current directory).
Produces bearing_block.FCStd.

Usage:
  OUTDIR=/tmp/out freecadcmd build_bearing_block.py
"""

import os
import sys
from pathlib import Path

import FreeCAD
import Part
import Sketcher

outdir = os.environ.get("OUTDIR", ".")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# === Document ===
doc = FreeCAD.newDocument("BearingBlock")

# === Spreadsheet Parameters ===
sheet = doc.addObject("Spreadsheet::Sheet", "Params")

_PARAMS = [
    (1, "BaseLength", "100", "BaseLength"),
    (2, "BaseWidth", "60", "BaseWidth"),
    (3, "BaseHeight", "10", "BaseHeight"),
    (4, "BossDiameter", "40", "BossDiameter"),
    (5, "BossHeight", "20", "BossHeight"),
    (6, "BoreDiameter", "20", "BoreDiameter"),
    (7, "MountHoleDiameter", "8", "MountHoleDiameter"),
    (8, "MountHoleInsetX", "15", "MountHoleInsetX"),
    (9, "MountHoleInsetY", "15", "MountHoleInsetY"),
    (10, "BossFilletRadius", "5", "BossFilletRadius"),
    (11, "BaseChamfer", "1", "BaseChamfer"),
    (12, "BossChamfer", "2", "BossChamfer"),
]
for row, label, value, alias in _PARAMS:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", value)
    sheet.setAlias(f"B{row}", alias)

_COMPUTED = [
    (14, "HalfBaseLength", "=BaseLength / 2", "HalfBaseLength"),
    (15, "HalfBaseWidth", "=BaseWidth / 2", "HalfBaseWidth"),
    (16, "BossRadius", "=BossDiameter / 2", "BossRadius"),
    (17, "BoreRadius", "=BoreDiameter / 2", "BoreRadius"),
    (18, "MountHoleRadius", "=MountHoleDiameter / 2", "MountHoleRadius"),
]
for row, label, formula, alias in _COMPUTED:
    sheet.set(f"A{row}", label)
    sheet.set(f"B{row}", formula)
    sheet.setAlias(f"B{row}", alias)

doc.recompute()

# Read values for initial geometry placement
BASE_L = float(sheet.get("B1"))
BASE_W = float(sheet.get("B2"))
BASE_H = float(sheet.get("B3"))
BOSS_D = float(sheet.get("B4"))
BOSS_H = float(sheet.get("B5"))
BORE_D = float(sheet.get("B6"))
MOUNT_D = float(sheet.get("B7"))
MOUNT_IX = float(sheet.get("B8"))
MOUNT_IY = float(sheet.get("B9"))

log(f"Params: {BASE_L=}, {BASE_W=}, {BASE_H=}, {BOSS_D=}, {BOSS_H=}")

# === Part Design Body ===
body = doc.addObject("PartDesign::Body", "Body")

# --- 1. Base Pad: rectangle on XY plane ---
# PartDesign Body's Origin has features: [X_Axis, Y_Axis, Z_Axis, XY_Plane, XZ_Plane, YZ_Plane]
base_sk = body.newObject("Sketcher::SketchObject", "BaseSketch")
base_sk.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]  # XY_Plane
base_sk.MapMode = "FlatFace"

# Rectangle centered on origin: 4 lines
hl, hw = BASE_L / 2, BASE_W / 2
bot = base_sk.addGeometry(Part.LineSegment(FreeCAD.Vector(-hl, -hw, 0), FreeCAD.Vector(hl, -hw, 0)))
right = base_sk.addGeometry(Part.LineSegment(FreeCAD.Vector(hl, -hw, 0), FreeCAD.Vector(hl, hw, 0)))
top = base_sk.addGeometry(Part.LineSegment(FreeCAD.Vector(hl, hw, 0), FreeCAD.Vector(-hl, hw, 0)))
left = base_sk.addGeometry(Part.LineSegment(FreeCAD.Vector(-hl, hw, 0), FreeCAD.Vector(-hl, -hw, 0)))

# Chain coincident
for a, b in [(bot, right), (right, top), (top, left), (left, bot)]:
    base_sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))

# Orientation
for g in [bot, top]:
    base_sk.addConstraint(Sketcher.Constraint("Horizontal", g))
for g in [left, right]:
    base_sk.addConstraint(Sketcher.Constraint("Vertical", g))

# Symmetric about origin: constrain distances from origin to corners
idx = base_sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, bot, 2, hl))
base_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseLength")
idx = base_sk.addConstraint(Sketcher.Constraint("DistanceX", bot, 1, -1, 1, hl))
base_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseLength")
idx = base_sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, right, 2, hw))
base_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseWidth")
idx = base_sk.addConstraint(Sketcher.Constraint("DistanceY", bot, 1, -1, 1, hw))
base_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseWidth")

doc.recompute()
log(f"BaseSketch: {base_sk.ConstraintCount} constraints, FullyConstrained={base_sk.FullyConstrained}")
assert base_sk.FullyConstrained, "BaseSketch not fully constrained!"

# Pad
base_pad = body.newObject("PartDesign::Pad", "BasePad")
base_pad.Profile = base_sk
base_pad.setExpression("Length", "Params.BaseHeight")
doc.recompute()
log(f"BasePad: Shape valid={base_pad.Shape.isValid()}, Volume={base_pad.Shape.Volume:.1f}")

# --- 2. Boss Pad: circle on top face of base ---
# Find the top face of the base pad (face with highest Z center)
base_shape = base_pad.Shape
top_face_idx = max(range(1, len(base_shape.Faces) + 1), key=lambda i: base_shape.Faces[i - 1].CenterOfMass.z)
log(f"Base pad top face: Face{top_face_idx} (z={base_shape.Faces[top_face_idx - 1].CenterOfMass.z})")

boss_sk = body.newObject("Sketcher::SketchObject", "BossSketch")
boss_sk.AttachmentSupport = [(base_pad, f"Face{top_face_idx}")]
boss_sk.MapMode = "FlatFace"

boss_circle = boss_sk.addGeometry(Part.Circle(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), BOSS_D / 2))
boss_sk.addConstraint(Sketcher.Constraint("Coincident", boss_circle, 3, -1, 1))
idx = boss_sk.addConstraint(Sketcher.Constraint("Radius", boss_circle, BOSS_D / 2))
boss_sk.setExpression(f"Constraints[{idx}]", "Params.BossRadius")

doc.recompute()
log(f"BossSketch: FullyConstrained={boss_sk.FullyConstrained}")
assert boss_sk.FullyConstrained, "BossSketch not fully constrained!"

boss_pad = body.newObject("PartDesign::Pad", "BossPad")
boss_pad.Profile = boss_sk
boss_pad.setExpression("Length", "Params.BossHeight")
doc.recompute()
log(f"BossPad: Volume={boss_pad.Shape.Volume:.1f}")

# --- 3. Central Bore: through pocket on top face of boss ---
boss_shape = boss_pad.Shape
bore_face_idx = max(range(1, len(boss_shape.Faces) + 1), key=lambda i: boss_shape.Faces[i - 1].CenterOfMass.z)

bore_sk = body.newObject("Sketcher::SketchObject", "BoreSketch")
bore_sk.AttachmentSupport = [(boss_pad, f"Face{bore_face_idx}")]
bore_sk.MapMode = "FlatFace"

bore_circle = bore_sk.addGeometry(Part.Circle(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), BORE_D / 2))
bore_sk.addConstraint(Sketcher.Constraint("Coincident", bore_circle, 3, -1, 1))
idx = bore_sk.addConstraint(Sketcher.Constraint("Radius", bore_circle, BORE_D / 2))
bore_sk.setExpression(f"Constraints[{idx}]", "Params.BoreRadius")

doc.recompute()
assert bore_sk.FullyConstrained, "BoreSketch not fully constrained!"

bore_pocket = body.newObject("PartDesign::Pocket", "CentralBore")
bore_pocket.Profile = bore_sk
bore_pocket.Type = 1  # Through All
doc.recompute()
log(f"CentralBore: Volume={bore_pocket.Shape.Volume:.1f}")

# --- 4. Mounting Holes: 4 circles on base plate top face ---
# Find the large flat face at z=BaseHeight (base top, excluding boss area)
holes_shape = bore_pocket.Shape
candidate_faces = []
for i, f in enumerate(holes_shape.Faces, 1):
    if abs(f.CenterOfMass.z - BASE_H) < 0.1 and f.Surface.isPlanar():
        candidate_faces.append((i, f))
holes_face_idx = max(candidate_faces, key=lambda x: x[1].Area)[0]
log(f"Mounting holes face: Face{holes_face_idx}")

holes_sk = body.newObject("Sketcher::SketchObject", "MountHolesSketch")
holes_sk.AttachmentSupport = [(bore_pocket, f"Face{holes_face_idx}")]
holes_sk.MapMode = "FlatFace"

# 4 holes at symmetric insets from corners
hole_positions = [
    (-BASE_L / 2 + MOUNT_IX, -BASE_W / 2 + MOUNT_IY),
    (BASE_L / 2 - MOUNT_IX, -BASE_W / 2 + MOUNT_IY),
    (BASE_L / 2 - MOUNT_IX, BASE_W / 2 - MOUNT_IY),
    (-BASE_L / 2 + MOUNT_IX, BASE_W / 2 - MOUNT_IY),
]
hole_geos = []
for hx, hy in hole_positions:
    h = holes_sk.addGeometry(Part.Circle(FreeCAD.Vector(hx, hy, 0), FreeCAD.Vector(0, 0, 1), MOUNT_D / 2))
    hole_geos.append(h)

# Equal radius, bind first to spreadsheet
for i in range(1, len(hole_geos)):
    holes_sk.addConstraint(Sketcher.Constraint("Equal", hole_geos[0], hole_geos[i]))
idx = holes_sk.addConstraint(Sketcher.Constraint("Radius", hole_geos[0], MOUNT_D / 2))
holes_sk.setExpression(f"Constraints[{idx}]", "Params.MountHoleRadius")

# Position hole 0 explicitly, then mirror the rest via Symmetric.
# hole 0: top-right    (+insetX, +insetY) — positioned by DistanceX/DistanceY
# hole 1: top-left     (-insetX, +insetY) — mirror of hole 0 about Y axis
# hole 2: bottom-left  (-insetX, -insetY) — mirror of hole 1 about X axis
# hole 3: bottom-right (+insetX, -insetY) — mirror of hole 0 about X axis
idx = holes_sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, hole_geos[0], 3, BASE_L / 2 - MOUNT_IX))
holes_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseLength - Params.MountHoleInsetX")
idx = holes_sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, hole_geos[0], 3, BASE_W / 2 - MOUNT_IY))
holes_sk.setExpression(f"Constraints[{idx}]", "Params.HalfBaseWidth - Params.MountHoleInsetY")

# Symmetric constraints — MUST use the 5-arg form (line symmetry about an axis).
# The 6-arg form (with a PointPos) creates POINT symmetry (180° rotation about
# a point), which is a different constraint. See GeoEnum.h:
#   -1 = HAxis (X axis, horizontal) — mirror about X axis flips Y
#   -2 = VAxis (Y axis, vertical)   — mirror about Y axis flips X
holes_sk.addConstraint(Sketcher.Constraint("Symmetric", hole_geos[0], 3, hole_geos[1], 3, -2))  # about Y axis
holes_sk.addConstraint(Sketcher.Constraint("Symmetric", hole_geos[1], 3, hole_geos[2], 3, -1))  # about X axis
holes_sk.addConstraint(Sketcher.Constraint("Symmetric", hole_geos[0], 3, hole_geos[3], 3, -1))  # about X axis

doc.recompute()
log(f"MountHolesSketch: {holes_sk.ConstraintCount} constraints, FullyConstrained={holes_sk.FullyConstrained}")
# Log actual solved positions to verify symmetry constraints
for j, g in enumerate(hole_geos):
    center = holes_sk.Geometry[g].Center
    log(f"  Hole {j} (geo {g}): center=({center.x:.1f}, {center.y:.1f})")
assert holes_sk.FullyConstrained, "MountHolesSketch not fully constrained!"

holes_pocket = body.newObject("PartDesign::Pocket", "MountHoles")
holes_pocket.Profile = holes_sk
holes_pocket.Type = 1  # Through All
doc.recompute()
log(f"MountHoles: Volume={holes_pocket.Shape.Volume:.1f}")

# --- 5. Boss Fillet: edges at base-to-boss junction ---
# Find circular edges at z=BaseHeight with radius ≈ BossRadius
# These are where the cylindrical boss meets the base plate
fillet_shape = holes_pocket.Shape
boss_r = BOSS_D / 2
fillet_edges = []
for i, e in enumerate(fillet_shape.Edges, 1):
    if not isinstance(e.Curve, Part.Circle):
        continue
    if abs(e.Curve.Radius - boss_r) < 0.5 and abs(e.Curve.Center.z - BASE_H) < 0.5:
        fillet_edges.append(f"Edge{i}")

log(f"Boss fillet edges: {fillet_edges}")
assert fillet_edges, "No edges found for boss fillet"

boss_fillet = body.newObject("PartDesign::Fillet", "BossFillet")
boss_fillet.Base = (holes_pocket, fillet_edges)
# Set expression ONLY — the expression engine resolves the value from the
# spreadsheet. Setting .Radius to a Python float would bake a constant into
# the FCStd; the relationship must live inside FreeCAD.
boss_fillet.setExpression("Radius", "Params.BossFilletRadius")
doc.recompute()
log(f"BossFillet: Volume={boss_fillet.Shape.Volume:.1f}")

# --- 6. Base Chamfer: top edges of base plate perimeter ---
# Find straight edges at z=BaseHeight on the outer rectangular perimeter
chamfer_shape = boss_fillet.Shape
base_chamfer_edges = []
for i, e in enumerate(chamfer_shape.Edges, 1):
    if not isinstance(e.Curve, Part.Line):
        continue
    v0, v1 = e.Vertexes[0].Point, e.Vertexes[1].Point
    if abs(v0.z - BASE_H) < 0.1 and abs(v1.z - BASE_H) < 0.1:
        on_perimeter = abs(abs(v0.x) - BASE_L / 2) < 0.1 or abs(abs(v0.y) - BASE_W / 2) < 0.1
        if on_perimeter:
            base_chamfer_edges.append(f"Edge{i}")

log(f"Base chamfer edges: {base_chamfer_edges}")
assert base_chamfer_edges, "No edges found for base chamfer"

base_chamfer = body.newObject("PartDesign::Chamfer", "BaseChamfer")
base_chamfer.Base = (boss_fillet, base_chamfer_edges)
base_chamfer.setExpression("Size", "Params.BaseChamfer")
doc.recompute()
log(f"BaseChamfer: Volume={base_chamfer.Shape.Volume:.1f}")

# --- 7. Boss Chamfer: top outer edge of boss cylinder ---
# Find circular edge at z = BaseHeight + BossHeight with radius ≈ BossRadius
boss_top_z = BASE_H + BOSS_H
boss_chamfer_shape = base_chamfer.Shape
boss_chamfer_edges = []
for i, e in enumerate(boss_chamfer_shape.Edges, 1):
    if not isinstance(e.Curve, Part.Circle):
        continue
    if abs(e.Curve.Center.z - boss_top_z) < 0.5 and abs(e.Curve.Radius - boss_r) < 1.0:
        boss_chamfer_edges.append(f"Edge{i}")

log(f"Boss chamfer edges: {boss_chamfer_edges}")
assert boss_chamfer_edges, "No edges found for boss chamfer"

boss_chamfer = body.newObject("PartDesign::Chamfer", "BossChamfer")
boss_chamfer.Base = (base_chamfer, boss_chamfer_edges)
boss_chamfer.setExpression("Size", "Params.BossChamfer")
doc.recompute()
log(f"BossChamfer: Volume={boss_chamfer.Shape.Volume:.1f}")

# === Final validation ===
final_shape = body.Shape
log(f"Final: Volume={final_shape.Volume:.1f}, Faces={len(final_shape.Faces)}, Edges={len(final_shape.Edges)}")
assert final_shape.isValid(), "Final shape is invalid!"

# Log face details for TechDraw References3D dimension attachment.
# The face indices here correspond to body.Shape.Faces[i-1] and are
# referenced as "Face{i}" in References3D tuples.
for i, f in enumerate(final_shape.Faces, 1):
    surface_type = type(f.Surface).__name__
    com = f.CenterOfMass
    extra = ""
    if surface_type == "Cylinder":
        extra = f" R={f.Surface.Radius:.1f}"
    elif surface_type == "Toroid":
        extra = f" R={f.Surface.MajorRadius:.1f}/{f.Surface.MinorRadius:.1f}"
    log(f"  Face{i}: {surface_type} area={f.Area:.1f} center=({com.x:.1f},{com.y:.1f},{com.z:.1f}){extra}")

# === Save ===
fcstd_path = os.path.join(outdir, "bearing_block.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
log(f"FCStd: {Path(fcstd_path).stat().st_size} bytes")

os._exit(0)
