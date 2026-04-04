"""
Build a parametric L-bracket using PartDesign workbench with spreadsheet-driven dimensions.

Demonstrates the Sketcher + PartDesign workflow:
1. Spreadsheet with named parameters
2. Sketch on XY plane → Pad (base plate)
3. Sketch on top face → Pad (vertical wall)
4. Sketch on top face → Pocket (mounting holes)
5. Fillet on inside corner

Runs inside freecadcmd (no GUI needed for modeling).
Output directory is read from OUTDIR env var (default: current directory).

Usage:
  OUTDIR=/tmp/out freecadcmd build_bracket.py
"""

import os
import sys
from pathlib import Path

import FreeCAD as App
import Part
import Sketcher

outdir = os.environ.get("OUTDIR", ".")

# === Document and Body ===
doc = App.newDocument("Bracket")
body = doc.addObject("PartDesign::Body", "Body")

# === Spreadsheet with parameters ===
sheet = doc.addObject("Spreadsheet::Sheet", "Params")

params = {
    "base_length": 80.0,
    "base_width": 50.0,
    "base_thickness": 8.0,
    "wall_height": 60.0,
    "wall_thickness": 8.0,
    "hole_diameter": 6.0,
    "hole_inset": 12.0,
    "fillet_radius": 5.0,
}

for row, (name, value) in enumerate(params.items(), start=1):
    val_cell = f"B{row}"
    sheet.set(f"A{row}", name)
    sheet.set(val_cell, str(value))
    sheet.setAlias(val_cell, name)

doc.recompute()


def find_face(shape, normal_dir, position_test):
    """Find a face by normal direction and position test on its CenterOfMass."""
    for face in shape.Faces:
        n = face.Surface.Axis if hasattr(face.Surface, "Axis") else face.normalAt(0, 0)
        if n.isEqual(normal_dir, 0.01) and position_test(face.CenterOfMass):
            return face
    raise RuntimeError(f"No face found with normal {normal_dir} passing position test")


def find_face_index(feature, face):
    """Return 1-based Face index string for a face on a feature's shape."""
    for i, f in enumerate(feature.Shape.Faces):
        if f.isEqual(face):
            return f"Face{i + 1}"
    raise RuntimeError("Face not found on feature")


# ============================================================
# Step 1: Base plate — Sketch on XY plane → Pad
# ============================================================
sk1 = body.newObject("Sketcher::SketchObject", "Sketch_Base")
sk1.Support = [(body.Origin.OriginFeatures[3], "")]
sk1.MapMode = "FlatFace"

# Rectangle: 4 lines + coincident + horizontal/vertical constraints
sk1.addGeometry(Part.LineSegment(App.Vector(0, 0, 0), App.Vector(80, 0, 0)), False)
sk1.addGeometry(Part.LineSegment(App.Vector(80, 0, 0), App.Vector(80, 50, 0)), False)
sk1.addGeometry(Part.LineSegment(App.Vector(80, 50, 0), App.Vector(0, 50, 0)), False)
sk1.addGeometry(Part.LineSegment(App.Vector(0, 50, 0), App.Vector(0, 0, 0)), False)

for a, b in [(0, 1), (1, 2), (2, 3), (3, 0)]:
    sk1.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))

sk1.addConstraint(Sketcher.Constraint("Horizontal", 0))
sk1.addConstraint(Sketcher.Constraint("Horizontal", 2))
sk1.addConstraint(Sketcher.Constraint("Vertical", 1))
sk1.addConstraint(Sketcher.Constraint("Vertical", 3))

# Pin to origin
sk1.addConstraint(Sketcher.Constraint("Coincident", 0, 1, -1, 1))

# Dimensions with spreadsheet expressions
c_len = sk1.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 80.0))
sk1.setExpression(f"Constraints[{c_len}]", "Params.base_length")

c_wid = sk1.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, 50.0))
sk1.setExpression(f"Constraints[{c_wid}]", "Params.base_width")

doc.recompute()
assert sk1.FullyConstrained, f"Sketch_Base under-constrained ({sk1.ConstraintCount} constraints)"

# Pad
pad_base = body.newObject("PartDesign::Pad", "Pad_Base")
pad_base.Profile = sk1
pad_base.Length = 8.0
pad_base.setExpression("Length", "Params.base_thickness")
doc.recompute()

print(f"Base plate: {pad_base.Shape.Volume:.1f} mm^3", flush=True)

# ============================================================
# Step 2: Vertical wall — Sketch on top face of base → Pad
# ============================================================
top_face = find_face(pad_base.Shape, App.Vector(0, 0, 1), lambda com: com.z > 7.0)

sk2 = body.newObject("Sketcher::SketchObject", "Sketch_Wall")
sk2.Support = [(pad_base, find_face_index(pad_base, top_face))]
sk2.MapMode = "FlatFace"
doc.recompute()

# Rectangle at back edge (Y = base_width - wall_thickness to base_width)
sk2.addGeometry(Part.LineSegment(App.Vector(0, 42, 0), App.Vector(80, 42, 0)), False)
sk2.addGeometry(Part.LineSegment(App.Vector(80, 42, 0), App.Vector(80, 50, 0)), False)
sk2.addGeometry(Part.LineSegment(App.Vector(80, 50, 0), App.Vector(0, 50, 0)), False)
sk2.addGeometry(Part.LineSegment(App.Vector(0, 50, 0), App.Vector(0, 42, 0)), False)

for a, b in [(0, 1), (1, 2), (2, 3), (3, 0)]:
    sk2.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))
sk2.addConstraint(Sketcher.Constraint("Horizontal", 0))
sk2.addConstraint(Sketcher.Constraint("Horizontal", 2))
sk2.addConstraint(Sketcher.Constraint("Vertical", 1))
sk2.addConstraint(Sketcher.Constraint("Vertical", 3))

# Pin bottom-left corner X to origin
sk2.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, 0, 1, 0.0))

c_w2_len = sk2.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 80.0))
sk2.setExpression(f"Constraints[{c_w2_len}]", "Params.base_length")

# Top edge Y = base_width
c_w2_y_top = sk2.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, 2, 1, 50.0))
sk2.setExpression(f"Constraints[{c_w2_y_top}]", "Params.base_width")

# Bottom edge Y = base_width - wall_thickness
c_w2_y_bot = sk2.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, 0, 1, 42.0))
sk2.setExpression(f"Constraints[{c_w2_y_bot}]", "Params.base_width - Params.wall_thickness")

doc.recompute()
assert sk2.FullyConstrained, "Sketch_Wall under-constrained"

pad_wall = body.newObject("PartDesign::Pad", "Pad_Wall")
pad_wall.Profile = sk2
pad_wall.Length = 60.0
pad_wall.setExpression("Length", "Params.wall_height")
doc.recompute()

print(f"Wall: {pad_wall.Shape.Volume:.1f} mm^3", flush=True)

# ============================================================
# Step 3: Mounting holes — Sketch on top face of base → Pocket
# ============================================================
# Top of base plate: Z-normal face at Z = base_thickness, in front of wall
base_top = find_face(pad_wall.Shape, App.Vector(0, 0, 1), lambda com: 0.1 < com.z < 9.0 and com.y < 40.0)

sk3 = body.newObject("Sketcher::SketchObject", "Sketch_Holes")
sk3.Support = [(pad_wall, find_face_index(pad_wall, base_top))]
sk3.MapMode = "FlatFace"
doc.recompute()

# Two circles for mounting holes
sk3.addGeometry(Part.Circle(App.Vector(12, 12, 0), App.Vector(0, 0, 1), 3.0), False)
sk3.addGeometry(Part.Circle(App.Vector(68, 12, 0), App.Vector(0, 0, 1), 3.0), False)

c_h1_x = sk3.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, 0, 3, 12.0))
sk3.setExpression(f"Constraints[{c_h1_x}]", "Params.hole_inset")

c_h1_y = sk3.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, 0, 3, 12.0))
sk3.setExpression(f"Constraints[{c_h1_y}]", "Params.hole_inset")

c_h1_r = sk3.addConstraint(Sketcher.Constraint("Radius", 0, 3.0))
sk3.setExpression(f"Constraints[{c_h1_r}]", "Params.hole_diameter / 2")

c_h2_x = sk3.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, 1, 3, 68.0))
sk3.setExpression(f"Constraints[{c_h2_x}]", "Params.base_length - Params.hole_inset")

c_h2_y = sk3.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, 1, 3, 12.0))
sk3.setExpression(f"Constraints[{c_h2_y}]", "Params.hole_inset")

c_h2_r = sk3.addConstraint(Sketcher.Constraint("Radius", 1, 3.0))
sk3.setExpression(f"Constraints[{c_h2_r}]", "Params.hole_diameter / 2")

doc.recompute()
assert sk3.FullyConstrained, "Sketch_Holes under-constrained"

pocket_holes = body.newObject("PartDesign::Pocket", "Pocket_Holes")
pocket_holes.Profile = sk3
pocket_holes.Type = 1  # Through All
doc.recompute()

print(f"After holes: {pocket_holes.Shape.Volume:.1f} mm^3", flush=True)

# ============================================================
# Step 4: Fillet on inside corner between wall and base
# ============================================================
target_y = params["base_width"] - params["wall_thickness"]  # 42.0
target_z = params["base_thickness"]  # 8.0

fillet_edge_name = None
for i, edge in enumerate(pocket_holes.Shape.Edges):
    if edge.Length < 5:
        continue
    mid = edge.valueAt(edge.FirstParameter + (edge.LastParameter - edge.FirstParameter) / 2)
    if (
        abs(mid.y - target_y) < 1.0
        and abs(mid.z - target_z) < 1.0
        and abs(edge.Vertexes[0].Point.x - edge.Vertexes[1].Point.x) > 50
    ):
        fillet_edge_name = f"Edge{i + 1}"
        break

if fillet_edge_name:
    fillet = body.newObject("PartDesign::Fillet", "Fillet_Corner")
    fillet.Base = (pocket_holes, [fillet_edge_name])
    fillet.Radius = 5.0
    fillet.setExpression("Radius", "Params.fillet_radius")
    doc.recompute()
    print(f"Fillet applied: {fillet.Shape.Volume:.1f} mm^3", flush=True)
else:
    print("WARNING: Could not find fillet edge, skipping fillet", flush=True)

# === Export ===
fcstd_path = os.path.join(outdir, "bracket.FCStd")  # noqa: PTH118 — FreeCAD API expects str
doc.saveAs(fcstd_path)
print(f"Saved: {fcstd_path} ({Path(fcstd_path).stat().st_size} bytes)", flush=True)

sys.stdout.flush()
os._exit(0)
