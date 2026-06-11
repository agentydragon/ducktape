# FreeCAD Sketcher Reference

## Constraints

Geometry point refs: 1=start, 2=end, 3=center(circles). Origin: geometry index -1, point 1.

**Positional:** `Coincident` (pin points together), `PointOnObject` (point on line/circle), `Block` (freeze geometry).

**Orientation:** `Horizontal`, `Vertical`, `Perpendicular` (two lines at 90°), `Parallel` (two lines same direction), `Tangent` (line tangent to circle/arc), `Angle` (specific angle between two lines).

**Dimensional:** `DistanceX` / `DistanceY` (horizontal/vertical distance between points), `Distance` (point-to-point or point-to-line), `Radius`, `Equal` (two segments same length).

**Common patterns:**

- Pin to origin: `Constraint('Coincident', idx, 1, -1, 1)`
- Chain lines: `Constraint('Coincident', line_a, 2, line_b, 1)`
- Perpendicular walls: `Constraint('Perpendicular', wall_a, wall_b)`
- Parallel edges: `Constraint('Parallel', edge_a, edge_b)`
- Fixed angle: `Constraint('Angle', line_a, line_b, radians)`
- Mirror about Y axis (flip X): `Constraint('Symmetric', g1, 3, g2, 3, -2)` — 5-arg form
- Mirror about X axis (flip Y): `Constraint('Symmetric', g1, 3, g2, 3, -1)` — 5-arg form

**Symmetric constraint:** The 5-arg form creates **line symmetry** (mirror about an axis). The 6-arg form `Symmetric(g1, p1, g2, p2, geoId, ptId)` creates **point symmetry** (180° rotation). Using 6 args with `(-1, 1)` or `(-2, 1)` mirrors about the **origin point** (0,0), not an axis. Axis indices: `-1` = HAxis (X axis), `-2` = VAxis (Y axis) — from `GeoEnum.h`.

After all geometry: `doc.recompute()`, assert `sk.FullyConstrained`.

**Redundant constraints:** Adding `Parallel` between two `Horizontal` lines is redundant and can cause `FullyConstrained=False` despite correct DOF count.

## Arc geometry

Use `Part.ArcOfCircle` for arcs. Point refs: 1=start, 2=end, 3=center.

```python
import math
arc = sk.addGeometry(Part.ArcOfCircle(
    Part.Circle(FreeCAD.Vector(cx, cy, 0), FreeCAD.Vector(0, 0, 1), radius),
    start_angle_radians, end_angle_radians
))
```

For fillet arcs connecting two lines, use `Tangent` constraints at the shared endpoints.
`Tangent` with point refs implies coincidence — do NOT add separate `Coincident` at the same
points, or the sketch will be over-constrained:

```python
# Arc tangent to right edge at their shared point
sk.addConstraint(Sketcher.Constraint("Tangent", right, 2, arc, 1))
# Arc tangent to top edge at their shared point
sk.addConstraint(Sketcher.Constraint("Tangent", arc, 2, top, 1))
```

## Spreadsheet-driven parameters

Use a `Spreadsheet::Sheet` to hold all input values with meaningful aliases, then bind
constraint values via `setExpression()`. This makes the sketch fully parametric.

```python
# Create spreadsheet with aliases
sheet = doc.addObject("Spreadsheet::Sheet", "Params")
sheet.set("A1", "Width"); sheet.set("B1", "120"); sheet.setAlias("B1", "Width")
sheet.set("A2", "Height"); sheet.set("B2", "80"); sheet.setAlias("B2", "Height")

# Computed intermediates via formulas (reference aliases, not cell addresses)
sheet.set("A3", "HalfWidth"); sheet.set("B3", "=Width / 2"); sheet.setAlias("B3", "HalfWidth")
doc.recompute()

# Bind constraint to spreadsheet cell
c_idx = sk.addConstraint(Sketcher.Constraint("DistanceX", bot, 1, bot, 2, 120.0))
sk.setExpression(f"Constraints[{c_idx}]", "Params.Width")
```

Cell aliases allow readable references like `Params.Width` instead of `Params.B1`. Formulas
can reference aliases: `"=Width / 2"`. Read values back: `float(sheet.get("B1"))`.

**Negative expressions:** `sk.setExpression(f"Constraints[{idx}]", "-Params.TabAngleRad")`.

**Angle unit:** `sk.setExpression(f"Constraints[{idx}]", "Params.Angle * 1 deg")` — raw
radian values without `* 1 deg` are treated as dimensionless, producing wrong angles.

**Angle constraint on one line:** `Constraint("Angle", line_idx, radians)` constrains angle
from X axis. For two-line angles, both lines must share a point.

## Modifying existing sketches

`sk.setDatum(constraint_index, FreeCAD.Units.Quantity(new_value))` — changes a constraint value
without rebuilding. Avoid removing geometry (shifts indices); convert to construction with
`sk.toggleConstruction(index)` instead.

Read solved geometry: `sk.Geometry[i].StartPoint/.EndPoint/.Center/.Radius`. Construction
flag: `sk.getConstruction(i)` (API typo is the correct name).
