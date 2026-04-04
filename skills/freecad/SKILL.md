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

## PartDesign Workbench (Parametric Solid Modeling)

The PartDesign workbench provides the standard parametric modeling workflow: sketch → pad → sketch on face → pocket → fillet. This is distinct from the `Part` module's boolean operations. See `build_bracket.py` for a complete worked example.

### Body and Spreadsheet setup

```python
body = doc.addObject("PartDesign::Body", "Body")
sheet = doc.addObject("Spreadsheet::Sheet", "Params")
sheet.set("B1", "80.0")
sheet.setAlias("B1", "base_length")
```

### Sketch attachment to faces

Attach sketches to the XY plane or to faces created by earlier operations:

```python
# XY plane (index 3 in Origin features: 0-2 are axes, 3-5 are planes)
sk.Support = [(body.Origin.OriginFeatures[3], "")]
sk.MapMode = "FlatFace"

# Face of a previous feature
sk.Support = [(pad_base, "Face6")]  # Use find_face_index() to get the right face
sk.MapMode = "FlatFace"
```

**The property is `Support`, not `AttachmentSupport`.** FreeCAD 0.21 uses `Support`.

### Finding faces programmatically

After padding, find the correct face by normal direction and position:

```python
for i, f in enumerate(pad.Shape.Faces):
    n = f.Surface.Axis  # face normal
    if n.isEqual(App.Vector(0, 0, 1), 0.01) and f.CenterOfMass.z > 7:
        face_name = f"Face{i + 1}"
        break
```

**Critical gotcha**: Face normals may be inverted in PartDesign. A face at Y=0 (which you'd expect to have outward normal (0,-1,0)) may report normal (0,+1,0). Always filter by position (CenterOfMass) in addition to normal direction.

**Critical gotcha**: After padding a base plate, both the top face (Z=thickness) and bottom face (Z=0) have +Z normals. A filter like `com.z < 9` will match Z=0 first. Always use `0.1 < com.z < 9` to exclude the bottom face.

### Pad (extrude)

```python
pad = body.newObject("PartDesign::Pad", "Pad_Base")
pad.Profile = sk1
pad.Length = 8.0
pad.setExpression("Length", "Params.base_thickness")  # Spreadsheet-driven
doc.recompute()
```

### Pocket (cut)

```python
pocket = body.newObject("PartDesign::Pocket", "Pocket_Holes")
pocket.Profile = sk_holes
pocket.Type = 1  # Through All
doc.recompute()
```

**Critical limitation (FreeCAD 0.21)**: PartDesign Pocket only works reliably on Z-normal faces (top/bottom). Pockets on Y-normal or X-normal side faces fail with "Recompute failed!" / "shape is invalid" due to face normal direction issues. The pocket computes the cut direction from the face normal, and inverted normals cause it to cut in the wrong direction (out of the body). **Workaround**: Sketch on a Z-normal face and use fixed depth, or use `Part::Cut` boolean operations for side-face cuts.

### Spreadsheet expressions for constraints

```python
c_idx = sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 80.0))
sk.setExpression(f"Constraints[{c_idx}]", "Params.base_length")
```

Always capture the return value of `addConstraint` — it's the constraint index needed for `setExpression`. Never hardcode constraint indices.

### Fillet

```python
fillet = body.newObject("PartDesign::Fillet", "Fillet_Corner")
fillet.Base = (previous_feature, ["Edge12"])
fillet.Radius = 5.0
fillet.setExpression("Radius", "Params.fillet_radius")
```

Find the correct edge by iterating `feature.Shape.Edges` and matching by midpoint position and length.

### Iterative modeling workflow

When building PartDesign models, work in a visual feedback loop:

1. Write/edit the build script
2. Run in Docker: `docker run --rm -v build_bracket.py:/work/build_bracket.py:ro -v /tmp/out:/output ghcr.io/agentydragon/freecad-test:latest bash -c "OUTDIR=/output freecadcmd /work/build_bracket.py"`
3. Render: `docker run --rm -v render_bracket.py:/work/render_bracket.py:ro -v /tmp/out:/output ... xvfb-run ... freecadcmd /work/render_bracket.py`
4. Visually inspect the rendered PNG
5. Fix issues and repeat from step 2
6. Once correct, commit goldens and run `bazel test`

For Bazel test debugging, inspect undeclared outputs:

```bash
ls $(bazel info bazel-testlogs)/skills/freecad/test_render_bracket/test.outputs/
```

### PartDesign gotchas

| Issue                                   | Fix                                                                                         |
| --------------------------------------- | ------------------------------------------------------------------------------------------- |
| `AttachmentSupport` not found           | Use `Support` property instead (FreeCAD 0.21 API)                                           |
| Face normals inverted                   | Filter faces by CenterOfMass position, not just normal direction                            |
| Bottom face matches Z-position filter   | Use `0.1 < com.z < max` to exclude Z=0 bottom face                                          |
| Pocket fails on side faces (Y/X normal) | Use Z-normal faces only, or Part::Cut for side cuts                                         |
| Pocket Through All cuts wrong direction | Direction follows face normal; inverted normals → wrong cut                                 |
| `os._exit(0)` swallows print output     | Add `flush=True` to all `print()` calls, or `sys.stdout.flush()` before `os._exit(0)`       |
| Render fails on sketches with "Shaded"  | Check `vo.listDisplayModes()` before setting `DisplayMode`; sketches don't support "Shaded" |
| Part same color as background           | Use distinct ShapeColor like `(0.55, 0.60, 0.70)`, not near-white `(0.75, 0.75, 0.80)`      |

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
