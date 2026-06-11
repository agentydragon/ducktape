# FreeCAD TechDraw Reference

## Page setup

`DrawPage` + `DrawSVGTemplate` (from `/usr/share/freecad/Mod/TechDraw/Templates/`). One
`DrawViewPart` with `Direction = Vector(0,0,1)` for top-down.

## Dimensions (entity-referenced)

**Always use entity-referenced `DrawViewDimension`** with `References2D` pointing to projected
edges. This is the only approach that produces parametric drawings — dimensions auto-update
when sketch geometry or spreadsheet parameters change.

Identify projected edges by geometric properties (radius, slope, position, length). Edge
indices vary between recomputes, so **match by geometry, not index**.

```python
vis_edges = view.getVisibleEdges()

def find_edge(predicate, desc):
    matches = [(i, e) for i, e in enumerate(vis_edges) if predicate(e)]
    if len(matches) != 1:
        raise AssertionError(f"Expected 1 edge matching {desc}, got {len(matches)}")
    return matches[0][0]

# Linear dimension (single edge → measures edge length)
dim = doc.addObject("TechDraw::DrawViewDimension", "RoomWidth")
page.addView(dim)
dim.Type = "DistanceX"
dim.References2D = [(view, f"Edge{bottom_edge_idx}")]
dim.X = 0    # view-local text offset
dim.Y = -10  # below the edge

# Linear dimension between two parallel edges
dim = doc.addObject("TechDraw::DrawViewDimension", "WallThickness")
page.addView(dim)
dim.Type = "DistanceY"
dim.References2D = [(view, f"Edge{outer_idx}"), (view, f"Edge{inner_idx}")]
dim.X = -15; dim.Y = 0

# Radius dimension (one circular edge ref)
dim = doc.addObject("TechDraw::DrawViewDimension", "FilletRadius")
page.addView(dim)
dim.Type = "Radius"
dim.References2D = [(view, f"Edge{fillet_edge}")]
dim.X = 20; dim.Y = 10

# Angle dimension (two line edge refs)
dim = doc.addObject("TechDraw::DrawViewDimension", "TabAngle")
page.addView(dim)
dim.Type = "Angle"
dim.References2D = [(view, f"Edge{bot_edge}"), (view, f"Edge{tab_edge}")]
dim.X = -12; dim.Y = -8
```

Supported `Type` values: `"Distance"`, `"DistanceX"`, `"DistanceY"`, `"Radius"`, `"Diameter"`,
`"Angle"`. Radius/Diameter need one circular edge ref; Angle needs two line edge refs. Linear
types accept one edge (measures its projected length) or two edges (measures distance between).

**You MUST set `dim.X` and `dim.Y`** — they default to `(0, 0)` (view center), so all text
overlaps without explicit placement.

See <parametric_sketch.py> for a full example. See <build_bearing_block_techdraw.py> for 3D
`References3D` dimensions on a Part Design body.

## Vertex-referenced dimensions (hole center to edge/corner)

`References2D` accepts `"VertexN"` references alongside `"EdgeN"`. This is how you dimension
from a circle center to a plate edge (e.g., mounting hole inset distances).

**Circle center vertices.** Each full circle edge in TechDraw generates 3 vertices: 2 at the
perimeter start/end point, and 1 at the geometric center. Find the center vertex by matching
coordinates from `getVertexBySelection("VertexN")` against `edge.Curve.Center`. Use
`len(view.getVisibleVertexes())` for the vertex count — do not probe speculatively with
try/except.

**Vertex-to-edge `DistanceX`.** Works correctly for measuring perpendicular distance from a
point to a vertical line edge:

```python
dim.Type = "DistanceX"
dim.References2D = [(view, "Vertex14"), (view, "Edge7")]  # hole center to plate edge
```

**Vertex-to-vertex `DistanceY`.** `DistanceY` between a vertex and a full-width horizontal
edge measures to the far endpoint, not the perpendicular projection. Use two vertices instead
(hole center + corner vertex on the same edge):

```python
dim.Type = "DistanceY"
dim.References2D = [(view, "Vertex14"), (view, "Vertex17")]  # hole center to corner
```

See <build_bearing_block_techdraw.py> for the full implementation including `find_vertex()`
and `find_circle_center_vertex()` helpers.

## 3D-referenced dimensions (References3D) — AVOID

`References3D` with `MeasureType="True"` is theoretically supported but **unreliable in
practice** (FreeCAD 1.1.0). Common failures: `2D references are corrupt`, `True dimension has
no 3D References`, tiny/zero-length leader lines. The value may be correct but the visual
rendering is broken.

**Preferred alternative:** place the dimension on a view where the feature has a visible
projected edge, then use `References2D`. For cylindrical features like a boss diameter, use
`DistanceX` on the profile edge in the front/side view with `FormatSpec="⌀%.0w"`:

```python
# Boss projects as a BSplineCurve with dx ≈ BossDiameter in the front view
boss_outline_idx = find_ranked_edge(front, ...)
dim.Type = "DistanceX"
dim.References2D = [(front, f"Edge{boss_outline_idx}")]
dim.FormatSpec = "⌀%.0w"  # shows "⌀40" for a 40mm boss
```

See <build_bearing_block_techdraw.py> `BossDiameter` for the full implementation.

## Chamfer dimensions

No built-in chamfer dimension type. Use `DistanceX` on the chamfer edge + append angle to `FormatSpec`:

```python
d = doc.addObject("TechDraw::DrawViewDimension", "ChamferDim")
page.addView(d)
d.Type = "DistanceX"  # measures horizontal leg, not hypotenuse
d.References2D = [(view, f"Edge{chamfer_edge_idx}")]
d.FormatSpec = "%.0f x45°"  # produces "2 x45°" for a 2mm chamfer
```

`Type="Distance"` on a 45° chamfer edge gives the hypotenuse (√2 × leg). `DistanceX`
extracts `fabs(dimVec.x)` — the horizontal leg — which is the correct chamfer size.

## `makeDistanceDim` — do not use

`TechDraw.makeDistanceDim()` creates point-based dimensions from hardcoded coordinates that
are not bound to projected entities. The resulting dimensions do not update when sketch
geometry changes. Always use `DrawViewDimension` with `References2D` instead.

## Centerlines and center marks

Use `makeCosmeticLine` on `DrawViewPart` to add centerlines (dash-dot lines indicating
symmetry axes) and center marks (small crosses at hole centers). Style `2` is the standard
ISO center line dash-dot pattern.

```python
# Centerline cross through a bore/boss center, extending beyond the feature
cl_extent = boss_d / 2 + 8
view.makeCosmeticLine(FreeCAD.Vector(0, -cl_extent, 0), FreeCAD.Vector(0, cl_extent, 0), 2)
view.makeCosmeticLine(FreeCAD.Vector(-cl_extent, 0, 0), FreeCAD.Vector(cl_extent, 0, 0), 2)

# Center marks on mounting holes (small cross at each hole center)
mark_size = hole_radius + 2
for i, e in enumerate(view.getVisibleEdges()):
    if isinstance(e.Curve, Part.Circle) and abs(e.Curve.Radius - hole_radius) < 1.0:
        cx, cy = e.Curve.Center.x, e.Curve.Center.y
        view.makeCosmeticLine(FreeCAD.Vector(cx, cy - mark_size, 0), FreeCAD.Vector(cx, cy + mark_size, 0), 2)
        view.makeCosmeticLine(FreeCAD.Vector(cx - mark_size, cy, 0), FreeCAD.Vector(cx + mark_size, cy, 0), 2)
```

**`makeCenterLine`** (not `makeCosmeticLine`) creates bisector-style centerlines between two
parallel edges. It requires 2+ edge references and does NOT work on a single circle — use
`makeCosmeticLine` for circle center crosses instead.

See <build_bearing_block_techdraw.py> for both patterns on the top view.

## Annotations

`DrawViewAnnotation` with `.Text`, `.X`, `.Y` (page mm), `.TextSize`, `.Font`, `.TextColor`,
`.Rotation`. Absolute page positioning.

**Page coordinate system:** Page Y increases **upward** (Y=0 is bottom). The conversion does
NOT invert Y:

```python
bb = feat.Shape.BoundBox
scx, scy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2
scale = float(view.Scale)
page_x = float(view.X) + (sketch_x - scx) * scale
page_y = float(view.Y) + (sketch_y - scy) * scale  # NOT minus — both Y-up
```

**Cast `view.X`, `view.Y`, `view.Scale` to `float()`** before arithmetic to avoid FreeCAD
`Quantity` unit mismatch errors.

**Unicode in DXF:** Unicode characters (e.g., `\u00b0`) corrupt in DXF export. Use ASCII
alternatives (`"60 deg"`).

## Multi-view TechDraw for 3D parts

Create multiple `DrawViewPart` objects on one page, each with a different `Direction` vector.
`Direction` is the **camera look direction** (where the camera points), NOT where the camera
is. `(0, 0, 1)` means the camera looks in +Z, i.e., it sees the top of the part (the face
with highest Z). `XDirection` defines which way is "right" in the projected view.

```python
front = doc.addObject("TechDraw::DrawViewPart", "FrontView")
front.Source = [body]
front.Direction = FreeCAD.Vector(0, -1, 0)
front.XDirection = FreeCAD.Vector(1, 0, 0)

right = doc.addObject("TechDraw::DrawViewPart", "RightView")
right.Source = [body]
right.Direction = FreeCAD.Vector(1, 0, 0)
right.XDirection = FreeCAD.Vector(0, 1, 0)

top = doc.addObject("TechDraw::DrawViewPart", "TopView")
top.Source = [body]
top.Direction = FreeCAD.Vector(0, 0, 1)

iso = doc.addObject("TechDraw::DrawViewPart", "IsoView")
iso.Source = [body]
iso.Direction = FreeCAD.Vector(1, -1, 1)
```

**Direction and XDirection must be perpendicular.** For axis-aligned directions, FreeCAD's
default XDirection algorithm may produce a degenerate projection CS — set it explicitly:

| Direction    | XDirection  | View       |
| ------------ | ----------- | ---------- |
| `(0, -1, 0)` | `(1, 0, 0)` | Front      |
| `(1, 0, 0)`  | `(0, 1, 0)` | Right side |
| `(-1, 0, 0)` | `(0, 1, 0)` | Left side  |
| `(0, 0, 1)`  | `(1, 0, 0)` | Top        |
| `(0, 0, -1)` | `(1, 0, 0)` | Bottom     |

**`getVisibleEdges()` returns edges in unscaled model coordinates** — compare radii against
model radius directly, not `radius * scale`.

**Cylinder projections produce BSplineCurves, not Lines.** Don't filter edges with
`isinstance(e.Curve, Part.Line)` for edges on cylindrical features — match by geometric
extent (`_edge_dx`, `_edge_dy`) regardless of curve type.

## View computation: waiting for TechDraw HLR

TechDraw runs Hidden Line Removal (HLR) asynchronously via `QtConcurrent` threads. After
`doc.recompute()`, the HLR thread starts but `recompute()` returns immediately.

**Why `processEvents()` is required:** The `QFutureWatcher::finished` Qt signal dispatches
HLR completion back to the main thread. Without calling `qapp.processEvents()`, this signal
is never delivered and `getVisibleEdges()` stays empty forever. A bare `time.sleep()` will
NOT work.

**Preferred approach — poll `getVisibleEdges()`:**

```python
def wait_for_view(view, timeout=15.0, poll_interval=0.05):
    """Poll until TechDraw view has visible edges, processing Qt events."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if qapp:
            qapp.processEvents()
        if len(view.getVisibleEdges()) > 0:
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"TechDraw view not ready after {timeout}s")

doc.recompute(None, True, True)
wait_for_view(view)             # typically completes in <2s
doc.recompute(None, True, True) # settle dimensions
pump(0.5)                       # short fixed pump for annotations
```

**Not available from Python** (C++ only in FreeCAD 1.1.0): `waitingForHlr()`,
`waitingForFaces()`, `waitingForResult()`. The `getVisibleEdges()` check is the best
Python-accessible readiness indicator.

**For 3D viewport rendering** (not TechDraw): no edge-based readiness indicator — use
`pump()` with conservative fixed durations and `processEvents()`.

The FCStd caches computed view edges when saved during a GUI session. Freshly created views
always require event pumping.
