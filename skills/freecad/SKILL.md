---
name: freecad-sketcher
description: Use this skill for parametric 2D/3D technical drawings using FreeCAD Sketcher and TechDraw. Triggers when the user wants constrained parametric floor plans, mechanical sketches, layout diagrams, or any drawing where dimensions drive geometry. Also use when the user mentions FreeCAD, .FCStd files, Sketcher, TechDraw, parametric CAD, or technical drawings. Use this skill even for seemingly simple 2D layouts — the parametric constraint approach prevents coordinate drift and makes edits safe. Always read this skill before writing any FreeCAD scripting code.
---

# FreeCAD Sketcher + TechDraw Skill

## Philosophy

The FCStd is the artifact; images are derived previews. Work iteratively: open, edit, save, export, visually check, repeat. Every dimension is a constraint.

## Setup

```bash
add-apt-repository -y ppa:freecad-maintainers/freecad-stable
apt-get update && apt-get install -y freecad-python3 xvfb
pip install ezdxf[draw] --break-system-packages
```

FreeCAD Python: `sys.path.insert(0, '/usr/lib/freecad-python3/lib')`.

## Sketcher

One `Sketcher::SketchObject` holds all geometry and constraints. For simple examples, define dimensions as Python variables and pass them to constraints. For parametric designs, use a `Spreadsheet::Sheet` to drive constraint values via expressions. See <parametric_sketch.py> for a full example with spreadsheet binding, arcs, tangent constraints, and TechDraw dimensions.

### Constraints

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

After all geometry: `doc.recompute()`, assert `sk.FullyConstrained`.

### Arc geometry

Use `Part.ArcOfCircle` for arcs. Point refs: 1=start, 2=end, 3=center.

```python
import math
arc = sk.addGeometry(Part.ArcOfCircle(
    Part.Circle(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), radius),
    start_angle_radians, end_angle_radians
))
```

For fillet arcs connecting two lines, use `Tangent` constraints at the shared endpoints (tangent with point refs implies coincidence — do NOT add separate `Coincident` constraints at the same points, or the sketch will be over-constrained):

```python
# Arc tangent to right edge at their shared point
sk.addConstraint(Sketcher.Constraint("Tangent", right, 2, arc, 1))
# Arc tangent to top edge at their shared point
sk.addConstraint(Sketcher.Constraint("Tangent", arc, 2, top, 1))
```

### Spreadsheet-driven parameters

Use a `Spreadsheet::Sheet` to hold all input values with meaningful aliases, then bind constraint values to spreadsheet cells via `setExpression()`. This makes the sketch fully parametric — change a spreadsheet cell and the entire sketch updates.

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

Cell aliases (`sheet.setAlias("B1", "Width")`) allow readable references like `Params.Width` instead of `Params.B1`. Formulas in cells can reference aliases: `"=Width / 2"`. Read values back with `float(sheet.get("B1"))`.

**Negative expressions:** For constraints that need negated values (e.g., angle below horizontal), use arithmetic in the expression string: `sk.setExpression(f"Constraints[{idx}]", "-Params.TabAngleRad")`.

### Modifying existing sketches

`sk.setDatum(constraint_index, App.Units.Quantity(new_value))` — changes a constraint value without rebuilding. Avoid removing geometry (shifts indices); convert to construction with `sk.toggleConstruction(index)` instead.

Read solved geometry: `sk.Geometry[i].StartPoint/.EndPoint/.Center/.Radius`. Construction flag: `sk.getConstruction(i)` (API typo is the correct name).

## Part Features

TechDraw projects `Part::Feature` shapes via HLR. Shape topology rules:

- `Part.Face` from closed wire: works
- `Part.Compound` of Faces: works
- Open `Part.Wire` or loose-edge Compound: crashes with `NCollection_Array1::Create`

Open wall segments: make thin (0.3cm) rectangular Face strips. See `examples/01_sketch_and_parts.py`.

**Single compound, single view.** Put ALL faces in one `Part.Compound` → one `Part::Feature` → one `TechDraw::DrawViewPart`. Multiple features with multiple views lose relative positions because TechDraw centers each view's bounding box independently.

## TechDraw

`DrawPage` + `DrawSVGTemplate` (from `/usr/share/freecad/Mod/TechDraw/Templates/`). One `DrawViewPart` with `Direction = Vector(0,0,1)` for top-down.

### Dimensions (working, point-based)

`TechDraw.makeDistanceDim(view, dimType, fromPoint, toPoint)` — creates a `DrawViewDimension`. Points are unscaled 2D view-local coordinates (sketch coords minus shape bounding box center). Types: `'Distance'`, `'DistanceX'`, `'DistanceY'`. Values auto-compute from the projected geometry and render as dimension lines with extension lines in the DXF export.

**You MUST set `dim.X` and `dim.Y`** after creating the dimension. These properties control the dimension text position as view-local offsets (mm). They default to `(0, 0)` which maps to the view center — so if you create multiple dimensions without setting X/Y, all text will overlap at the view center. Set them to the view-local coordinates of the dimension line midpoint, computed from the same `fromPoint`/`toPoint` values passed to `TechDraw.makeDistanceDim`:

```python
bb = feat.Shape.BoundBox
cx, cy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2

# Width dim 15mm below bottom edge
d1_from = App.Vector(0 - cx, -15 - cy, 0)
d1_to = App.Vector(WIDTH - cx, -15 - cy, 0)
d1 = TechDraw.makeDistanceDim(view, "DistanceX", d1_from, d1_to)
page.addView(d1)
d1.X = (d1_from.x + d1_to.x) / 2
d1.Y = (d1_from.y + d1_to.y) / 2

# Height dim 15mm right of right edge
d2_from = App.Vector(WIDTH + 15 - cx, 0 - cy, 0)
d2_to = App.Vector(WIDTH + 15 - cx, HEIGHT - cy, 0)
d2 = TechDraw.makeDistanceDim(view, "DistanceY", d2_from, d2_to)
page.addView(d2)
d2.X = (d2_from.x + d2_to.x) / 2
d2.Y = (d2_from.y + d2_to.y) / 2
```

See `examples/02_techdraw_and_dims.py`.

### Dimensions (entity-referenced, for radii and edge-bound dims)

For radius/diameter dimensions, use `DrawViewDimension` with `Type = "Radius"` and `References2D` pointing to a projected arc/circle edge. TechDraw automatically renders "R<value>" with a leader line from center to circumference — do NOT manually create text annotations with hardcoded "R12" strings.

```python
# Identify projected edges by geometric properties (radius, slope, position).
# Edge indices vary between recomputes, so match by geometry, not index.
vis_edges = view.getVisibleEdges()
fillet_edge: int | None = None
hole_edge: int | None = None
for i, e in enumerate(vis_edges):
    if e.Curve.__class__.__name__ == "Circle":
        if abs(e.Curve.Radius - fillet_r) < 0.1:
            fillet_edge = i
        elif abs(e.Curve.Radius - hole_r) < 0.1:
            hole_edge = i

# Entity-referenced radius dimension
dim = doc.addObject("TechDraw::DrawViewDimension", "FilletRadius")
page.addView(dim)
dim.Type = "Radius"
dim.References2D = [(view, f"Edge{fillet_edge}")]
dim.X = 20  # view-local text offset
dim.Y = 10

# Entity-referenced angle dimension (between two adjacent lines)
dim = doc.addObject("TechDraw::DrawViewDimension", "TabAngle")
page.addView(dim)
dim.Type = "Angle"
dim.References2D = [(view, f"Edge{bot_edge}"), (view, f"Edge{tab_edge}")]
dim.X = -12; dim.Y = -8
```

Supported `Type` values: `"Distance"`, `"DistanceX"`, `"DistanceY"`, `"Radius"`, `"Diameter"`, `"Angle"`. Radius/Diameter need one circular edge ref; Angle needs two line edge refs.

`makeDistanceDim` (point-based) is still appropriate for linear dimensions (width, height, distances) where you control the measurement points. Use entity-referenced `DrawViewDimension` for radii, diameters, and edge-specific dimensions.

### Annotations

`DrawViewAnnotation` with `.Text`, `.X`, `.Y` (page mm), `.TextSize`, `.Font`, `.TextColor`, `.Rotation`. Absolute page positioning.

**Page coordinate system:** Page Y increases upward (Y=0 is bottom of page, Y=210 is top of A4). Sketch Y also increases upward. So the conversion does NOT invert Y:

```python
bb = feat.Shape.BoundBox
scx, scy = (bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2
scale = float(view.Scale)
page_x = float(view.X) + (sketch_x - scx) * scale
page_y = float(view.Y) + (sketch_y - scy) * scale  # NOT minus — both Y-up
```

**Cast `view.X`, `view.Y`, `view.Scale` to `float()`** before arithmetic to avoid FreeCAD `Quantity` unit mismatch errors when mixing with plain floats.

**Unicode in DXF:** Unicode characters (e.g., degree symbol `\u00b0`) corrupt in DXF export via `writeDXFPage`. Use ASCII alternatives (e.g., `"60 deg"` instead of `"60°"`).

## 3D Modeling and Rendering

### Building 3D models

Use `Part` primitives and boolean operations for solid geometry:

```python
import FreeCAD as App
import Part

cube = Part.makeBox(20, 20, 20, App.Vector(-10, -10, -10))
cylinder = Part.makeCylinder(5, 22, App.Vector(0, 0, -11), App.Vector(0, 0, 1))
result = cube.cut(cylinder)

feat = doc.addObject("Part::Feature", "CubeWithHole")
feat.Shape = result
```

See <build_cube_with_hole.py> for a complete example.

### Rendering FCStd to PNG

Use FreeCAD's GUI viewport under Xvfb for offscreen 3D rendering with perspective and lighting:

```bash
INPUT=/work/model.FCStd OUTDIR=/output \
  xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd render_fcstd.py
```

Key steps in the render script:

1. `FreeCADGui.showMainWindow()` — initialize offscreen GUI
2. Open the FCStd, set objects to `Shaded` display mode with `Two side` lighting
3. `view.viewIsometric()` + `view.fitAll()` for camera setup
4. `view.saveImage(path, 800, 600, "Current")` to capture
5. `os._exit(0)` to avoid Qt cleanup segfault

See <render_fcstd.py> for the full script.

## Export

Example scripts produce FCStd files. Use `export_page.py` to export to DXF, SVG, and PDF:

```bash
OUTDIR=. xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd parametric_rect.py  # → rect.FCStd
INPUT=rect.FCStd OUTDIR=. xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd export_page.py  # → rect.{dxf,svg,pdf}
```

Arguments are passed via env vars (`INPUT`, `OUTDIR`) because `freecadcmd` treats CLI args as files to open. See <export_page.py>. Output filenames derive from the input FCStd stem.

**DXF → PNG rendering:** `python3 render_dxf.py output.dxf output.png`. See <render_dxf.py>.

### Format comparison

| Format | API                                       | Strengths                                                       | Limitations                                           |
| ------ | ----------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------- |
| DXF    | `TechDraw.writeDXFPage(page, path)`       | CAD-compatible, editable in other CAD tools                     | R12/R14 only, font rendering depends on ezdxf for PNG |
| SVG    | `TechDrawGui.exportPageAsSvg(page, path)` | Vector, viewable in browsers, preserves template/fonts natively | Hatch patterns not exported (Qt SVG limitation)       |
| PDF    | `TechDrawGui.exportPageAsPdf(page, path)` | Print-ready, universal viewer support                           | Largest file size                                     |

### View computation: the processEvents discovery

TechDraw computes views in a background Qt thread/timer. After `doc.recompute()`, you MUST pump Qt events:

```python
from PySide2 import QtWidgets
app = QtWidgets.QApplication.instance()
for _ in range(50):
    app.processEvents()
    time.sleep(0.1)
```

Without this, `time.sleep()` alone does nothing — the Qt event loop is stalled and the background computation never runs. This is the single most important gotcha for headless TechDraw. Views will show 0 edges without event pumping, regardless of how long you sleep.

The FCStd caches computed view edges when saved during a GUI session. Reloading a previously-cached file shows edges immediately. But freshly created views always require event pumping.

## Gotchas

| Issue                                | Fix                                                                                                                                                               |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TechDraw 0 edges                     | Pump Qt events with `processEvents()` loop after recompute                                                                                                        |
| Open Wire / loose edges              | Use Faces only — open topology crashes HLR projector                                                                                                              |
| Multiple views lose positions        | Single compound, single view                                                                                                                                      |
| FreeCAD import                       | `sys.path.insert(0, '/usr/lib/freecad-python3/lib')`                                                                                                              |
| `getConstruction` typo               | Correct API name                                                                                                                                                  |
| Property "mm" suffix                 | `re.sub(r'\s*mm$', '', str(val))` before numeric use                                                                                                              |
| Under-constrained sketch             | Add constraints until `FullyConstrained == True`                                                                                                                  |
| Removing geometry                    | Shifts indices — use `toggleConstruction` instead                                                                                                                 |
| DXF Y convention                     | CAD Y-up: sketch Y=0 (e.g. window wall) at bottom of render                                                                                                       |
| Annotation placement                 | Absolute page coords; use bounding box center math to convert from sketch coords                                                                                  |
| Dim text overlaps at center          | `makeDistanceDim` defaults `X=0, Y=0` (view center) — must set `dim.X`/`dim.Y`                                                                                    |
| Dim text on dimension line           | Offset `dim.X`/`dim.Y` so text clears the dimension line, especially for vertical dims where horizontal text can sit on the line                                  |
| Duplicate dims in DXF                | `writeDXFPage` may emit each dimension twice — known FreeCAD export artifact                                                                                      |
| Dim "larger than page" warns         | Dimension geometry extends beyond template bounds — cosmetic, does not break DXF                                                                                  |
| Tangent + Coincident overlap         | `Tangent` with point refs (e.g., `line, 2, arc, 1`) implies coincidence — adding a separate `Coincident` at the same points over-constrains the sketch            |
| Angle constraint on one line         | `Constraint("Angle", line_idx, radians)` constrains angle from X axis. For two-line angles, both lines must share a point                                         |
| Angle expression needs `deg` unit    | `setExpression("Constraints[N]", "Params.Angle * 1 deg")` — raw radian values without unit annotation are treated as dimensionless, producing wrong angles        |
| TechDraw edge Y is inverted          | `getVisibleEdges()` returns view-local coords with Y inverted (edge_y = cy - sketch_y). Match edges by geometric properties (slope, radius) not raw coordinates   |
| `print()` invisible in freecadcmd    | freecadcmd may buffer stdout; use `print(msg, file=sys.stderr, flush=True)` for debug output visible in Docker logs                                               |
| PDF font subset names                | PDF exports embed fonts with non-deterministic subset prefixes (e.g., `QNAAAA+DejaVuSans`). Golden files must come from the same environment (RBE) as tests       |
| Annotation Y direction               | Page Y increases **upward** (Y=0 is bottom). Use `view.Y + (sketch_y - cy) * scale`, NOT `view.Y - ...`. The minus formula in old docs is wrong                   |
| Quantity unit mismatch               | `view.X`, `view.Y`, `view.Scale` return FreeCAD Quantity objects. Cast to `float()` before mixing with plain Python floats in arithmetic                          |
| Unicode in DXF                       | Unicode chars (e.g., `\u00b0` degree symbol) corrupt in DXF export. Use ASCII alternatives (`"60 deg"`)                                                           |
| Radius dims as text annotations      | Do NOT manually write "R12" text annotations. Use `DrawViewDimension` with `Type="Radius"` and `References2D=[(view, "EdgeN")]` for proper auto-computed callouts |
| Redundant constraints confuse solver | Adding `Parallel` between two `Horizontal` lines is redundant and can cause `FullyConstrained=False` despite correct DOF count                                    |
| Open wires for geometric features    | Features like tabs/gussets must be closed faces integrated into the profile contour, not separate open wires rendered as thin strips                              |

### Visual inspection checklist

After generating a TechDraw PNG, visually inspect the output for:

- **Text overlapping geometry** — dimension labels sitting on top of edges or other labels
- **Text on dimension lines** — especially vertical dimensions where horizontal text can land directly on the arrow line; offset text away from the line
- **Extension line overshoot** — lines extending well beyond the geometry they reference
- **Cramped or cut-off labels** — text too close to drawing edges or clipped by the viewport
