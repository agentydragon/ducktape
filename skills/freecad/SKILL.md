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

One `Sketcher::SketchObject` holds all geometry and constraints. Define dimensions as Python variables; pass them to constraints. See `examples/01_sketch_and_parts.py` for a complete worked example.

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

**TODO:** Entity-referenced dimensions via `dim.References2D = [(view, 'EdgeN')]` that bind to specific projected edges rather than points. This would survive sketch edits without recomputing point positions in Python.

### Annotations

`DrawViewAnnotation` with `.Text`, `.X`, `.Y` (page mm), `.TextSize`, `.Font`, `.TextColor`, `.Rotation`. Absolute page positioning.

Sketch-to-page coordinate conversion (for a single-compound view):

```python
bb = feat.Shape.BoundBox
scx, scy = (bb.XMin+bb.XMax)/2, (bb.YMin+bb.YMax)/2
page_x = view.X + (sketch_x - scx) * view.Scale
page_y = view.Y - (sketch_y - scy) * view.Scale
```

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

| Issue                         | Fix                                                                                                                              |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| TechDraw 0 edges              | Pump Qt events with `processEvents()` loop after recompute                                                                       |
| Open Wire / loose edges       | Use Faces only — open topology crashes HLR projector                                                                             |
| Multiple views lose positions | Single compound, single view                                                                                                     |
| FreeCAD import                | `sys.path.insert(0, '/usr/lib/freecad-python3/lib')`                                                                             |
| `getConstruction` typo        | Correct API name                                                                                                                 |
| Property "mm" suffix          | `re.sub(r'\s*mm$', '', str(val))` before numeric use                                                                             |
| Under-constrained sketch      | Add constraints until `FullyConstrained == True`                                                                                 |
| Removing geometry             | Shifts indices — use `toggleConstruction` instead                                                                                |
| DXF Y convention              | CAD Y-up: sketch Y=0 (e.g. window wall) at bottom of render                                                                      |
| Annotation placement          | Absolute page coords; use bounding box center math to convert from sketch coords                                                 |
| Dim text overlaps at center   | `makeDistanceDim` defaults `X=0, Y=0` (view center) — must set `dim.X`/`dim.Y`                                                   |
| Dim text on dimension line    | Offset `dim.X`/`dim.Y` so text clears the dimension line, especially for vertical dims where horizontal text can sit on the line |
| Duplicate dims in DXF         | `writeDXFPage` may emit each dimension twice — known FreeCAD export artifact                                                     |
| Dim "larger than page" warns  | Dimension geometry extends beyond template bounds — cosmetic, does not break DXF                                                 |

### Visual inspection checklist

After generating a TechDraw PNG, visually inspect the output for:

- **Text overlapping geometry** — dimension labels sitting on top of edges or other labels
- **Text on dimension lines** — especially vertical dimensions where horizontal text can land directly on the arrow line; offset text away from the line
- **Extension line overshoot** — lines extending well beyond the geometry they reference
- **Cramped or cut-off labels** — text too close to drawing edges or clipped by the viewport
