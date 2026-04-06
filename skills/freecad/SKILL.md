---
name: freecad-sketcher
description: Use this skill for parametric 2D/3D technical drawings using FreeCAD Sketcher and TechDraw. Triggers when the user wants constrained parametric floor plans, mechanical sketches, layout diagrams, or any drawing where dimensions drive geometry. Also use when the user mentions FreeCAD, .FCStd files, Sketcher, TechDraw, parametric CAD, or technical drawings. Use this skill even for seemingly simple 2D layouts — the parametric constraint approach prevents coordinate drift and makes edits safe. Always read this skill before writing any FreeCAD scripting code.
---

# FreeCAD Sketcher + TechDraw Skill

## Philosophy

The FCStd is the artifact; images are derived previews. Work iteratively: open, edit, save, export, visually check, repeat. Every dimension is a constraint.

**Parametric-first**: Spreadsheet parameters are the single source of truth (SSOT). All sketch constraints must be bound to spreadsheet cells via `setExpression()`. All TechDraw dimensions must reference projected entities (`References2D`) so they auto-update when parameters change. Never use hardcoded coordinates, one-shot Python variables, or point-based `makeDistanceDim` — these create models where changing a parameter does not propagate to the drawing.

## Setup

**Target version: FreeCAD 1.1.0.** All examples and tests are validated against FreeCAD 1.1.0 (AppImage, Python 3.11). The API surface (Part Design, Sketcher, TechDraw) changed between 0.21 and 1.0 (e.g., Topological Naming Protection, PySide6 migration). Pin to 1.1.0 to avoid compatibility surprises:

```bash
# Recommended: FreeCAD 1.1.0 AppImage (self-contained, no system package conflicts)
wget -q "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.0/FreeCAD_1.1.0-Linux-x86_64-py311.AppImage" -O /opt/FreeCAD.AppImage
chmod +x /opt/FreeCAD.AppImage

# Also need Xvfb for headless GUI/TechDraw/rendering
apt-get install -y xvfb

# For DXF→PNG rendering (outside FreeCAD)
pip install ezdxf[draw] --break-system-packages
```

Alternatively, use the system package (may be older): `apt-get install -y freecad-python3 xvfb` with `sys.path.insert(0, '/usr/lib/freecad-python3/lib')`.

## Script Execution Model

FreeCAD has two binaries with very different lifecycle models:

| Binary       | Event loop    | Use for                                             |
| ------------ | ------------- | --------------------------------------------------- |
| `freecad`    | `exec()` runs | TechDraw, 3D rendering — anything needing the GUI   |
| `freecadcmd` | No `exec()`   | Headless solid geometry, DXF-only (no TechDraw GUI) |

**Always use `freecad` (GUI binary) for scripts that use `FreeCADGui`, `TechDraw`, or any Qt event pump.** The GUI binary enters `QApplication::exec()` cleanly and exits without crashing. `freecadcmd` never calls `exec()`, and its `QApplication` teardown triggers a Qt6 TLS use-after-free segfault on exit for any script that initialized the GUI. See <debug/qt_shutdown_segfault.md> for the full root cause analysis.

### GUI binary invocation pattern

Scripts run under the GUI binary must defer all work until after `exec()` starts, because module-level code runs during `processCmdLineFiles()` — before the event loop is live:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # find freecad_helpers.py alongside this script

import FreeCAD as App
import FreeCADGui as Gui
from freecad_helpers import init_gui, log, pump, wait_for_view

try:
    from PySide6.QtCore import QTimer
except ImportError:
    from PySide2.QtCore import QTimer

qapp = init_gui()


def _work() -> None:
    # ... all script logic here ...
    doc.setClosable(True)        # suppress GUI save dialog on close
    App.closeDocument(doc.Name)
    Gui.getMainWindow().close()  # triggers clean QApplication exit


QTimer.singleShot(0, _work)     # deferred: fires immediately after exec() starts
```

To run with Xvfb:

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
DISPLAY=:99 OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecad parametric_sketch.py
```

**Do not use `xvfb-run -a ... freecad script.py`** — `xvfb-run` hangs waiting for the GUI binary to exit in a way it never will cleanly.

### Headless invocation (freecadcmd)

Scripts that only do solid geometry (no `FreeCADGui`, no TechDraw) can run under `freecadcmd` with `QT_QPA_PLATFORM=offscreen` — no Xvfb needed:

```bash
QT_QPA_PLATFORM=offscreen OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecadcmd build_cube.py
```

### Bazel test fixtures

Tests use session-scoped fixtures from `conftest.py`:

- `freecad_gui(script, outdir, env)` — runs `freecad` binary with Xvfb (Xvfb started by `xvfb_display` fixture)
- `freecad_headless(script, outdir, env)` — runs `freecadcmd` with `QT_QPA_PLATFORM=offscreen`
- `xvfb_display` — session-scoped Xvfb process, yields display string (e.g. `":99"`)

No Docker is needed for scripts that use the AppImage directly.

### Fonts

FreeCAD bundles **osifont** (LGPL) in `<ResourceDir>/Mod/TechDraw/Resources/fonts/osifont-lgpl3fe.ttf`. This is the default font for TechDraw dimension text and annotations. It ships with every FreeCAD installation, making it a safe default that doesn't depend on system font availability.

For deterministic TechDraw exports across machines, register osifont with fontconfig so Qt can find it:

```bash
ln -sf /opt/squashfs-root/usr/Mod/TechDraw/Resources/fonts /usr/local/share/fonts/techdraw
fc-cache -f
```

Without this, TechDraw falls back to whichever system font Qt resolves first, which varies across machines and Docker workers, producing non-deterministic PDF/SVG exports (different glyph paths, different font subset prefixes).

The debug renderers (`render_debug_edges.py`, `render_debug_faces.py`) also use osifont for label text, loaded via `QFontDatabase.addApplicationFont()` from the FreeCAD resource directory.

## Sketcher

One `Sketcher::SketchObject` holds all geometry and constraints. For simple examples, define dimensions as Python variables and pass them to constraints. For parametric designs, use a `Spreadsheet::Sheet` to drive constraint values via expressions. See <parametric_sketch.py> for a full example with spreadsheet binding, arcs, tangent constraints, and TechDraw dimensions. See <build_compound.py> for compound shapes with wall shells.

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
- Mirror about Y axis (flip X): `Constraint('Symmetric', g1, 3, g2, 3, -2)` — 5-arg form
- Mirror about X axis (flip Y): `Constraint('Symmetric', g1, 3, g2, 3, -1)` — 5-arg form

**Symmetric constraint pitfall:** The 5-arg form creates **line symmetry** (mirror about an axis). The 6-arg form `Symmetric(g1, p1, g2, p2, geoId, ptId)` creates **point symmetry** (180° rotation about a point). Using `(-1, 1)` or `(-2, 1)` as the last two args mirrors about the **origin point**, not an axis — both resolve to the same (0,0) point. Axis indices: `-1` = HAxis (X axis), `-2` = VAxis (Y axis) — from `GeoEnum.h`.

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

All geometry must be closed faces — model walls as closed polygons tracing inner and outer outlines (shell approach). See <build_compound.py> for an L-shaped wall shell.

**Single compound, single view.** Put ALL faces in one `Part.Compound` → one `Part::Feature` → one `TechDraw::DrawViewPart`. Multiple features with multiple views lose relative positions because TechDraw centers each view's bounding box independently.

## TechDraw

`DrawPage` + `DrawSVGTemplate` (from `/usr/share/freecad/Mod/TechDraw/Templates/`). One `DrawViewPart` with `Direction = Vector(0,0,1)` for top-down.

### Dimensions (entity-referenced)

**Always use entity-referenced `DrawViewDimension`** with `References2D` pointing to projected edges. This is the only approach that produces parametric drawings — dimensions auto-update when sketch geometry or spreadsheet parameters change.

Identify projected edges by geometric properties (radius, slope, position, length). Edge indices vary between recomputes, so **match by geometry, not index**.

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

Supported `Type` values: `"Distance"`, `"DistanceX"`, `"DistanceY"`, `"Radius"`, `"Diameter"`, `"Angle"`. Radius/Diameter need one circular edge ref; Angle needs two line edge refs. Linear types accept one edge (measures its projected length) or two edges (measures distance between them).

**You MUST set `dim.X` and `dim.Y`** after creating the dimension — they default to `(0, 0)` (view center), so all text overlaps without explicit placement.

See <parametric_sketch.py> for a full example with radius, angle, and linear entity-referenced dimensions. See <build_compound.py> for entity-referenced dimensions on a compound floor plan. See <build_bearing_block_techdraw.py> for 3D References3D dimensions on a Part Design body.

### 3D-referenced dimensions (References3D + MeasureType="True")

For 3D Part Design models, `DrawViewDimension` supports measuring directly from 3D geometry via `References3D` and `MeasureType = "True"`. This is the proper way to dimension cylindrical features (boss diameter, bore diameter, hole diameter) because the 3D measurement is independent of the 2D projection.

```python
# Find the cylindrical face by geometric properties (not hardcoded index)
boss_cyl_face = find_3d_face(
    body.Tip.Shape,
    lambda f: type(f.Surface).__name__ == "Cylinder" and abs(f.Surface.Radius - 20) < 0.5,
    "boss cylinder R=20",
)  # returns "Face1" (or whichever matches)

d = doc.addObject("TechDraw::DrawViewDimension", "BossDiameter")
page.addView(d)
d.Type = "Diameter"
d.MeasureType = "True"
d.References2D = [(front_view, "")]     # view as context (empty sub-element)
d.References3D = [(body.Tip, boss_cyl_face)]  # 3D cylindrical face
```

What works with References3D (confirmed in FreeCAD 1.1.0 source, `DrawViewDimension.cpp`):

- **Cylinder diameter/radius**: single cylindrical face → auto-computes diameter
- **Edge lengths**: single 3D edge reference
- **Point-to-point**: two vertex references

What does NOT work (gap in `getTrueDimValue()`):

- **Face-to-face distance**: `Measurement.planePlaneDistance()` exists in the Measure module but `DrawViewDimension` doesn't call it for the `TwoPlanes` case. You can't dimension "distance between base bottom face and base top face" directly. Use projected-edge DistanceY as a workaround.

### Chamfer dimensions

There is no built-in chamfer dimension type in TechDraw. FreeCAD's GUI Extension commands (`CommandExtensionDims.cpp`) implement chamfer callouts by creating a `DistanceX` (or `DistanceY`) dimension on the chamfer edge and appending the angle to `FormatSpec`:

```python
d = doc.addObject("TechDraw::DrawViewDimension", "ChamferDim")
page.addView(d)
d.Type = "DistanceX"  # measures horizontal leg, not hypotenuse
d.References2D = [(view, f"Edge{chamfer_edge_idx}")]
d.FormatSpec = "%.0f x45°"  # produces "2 x45°" for a 2mm chamfer
```

`Type="Distance"` on a 45° chamfer edge gives the hypotenuse (√2 × leg), which is wrong. `DistanceX` extracts `fabs(dimVec.x)` — the horizontal leg — which is the correct chamfer size.

### `makeDistanceDim` — discouraged

**Do not use `TechDraw.makeDistanceDim()`**. It creates point-based dimensions from hardcoded coordinates that are not bound to projected entities. The resulting dimensions do not update when sketch geometry changes, breaking the parametric model. There is no SSOT — the dimension's measurement points are frozen at creation time. Always use `DrawViewDimension` with `References2D` instead, even for simple linear dimensions.

### Annotations

`DrawViewAnnotation` with `.Text`, `.X`, `.Y` (page mm), `.TextSize`, `.Font`, `.TextColor`, `.Rotation`. Absolute page positioning. See <parametric_sketch.py> for an example with annotation placement.

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

## Part Design Workbench

The Part Design workbench is FreeCAD's standard approach for solid modeling. It uses a feature tree inside a `PartDesign::Body` where each operation (Pad, Pocket, Fillet, Chamfer) builds on the previous one. See <build_bearing_block.py> for a full example with spreadsheet-driven parameters, and <build_bearing_block_techdraw.py> for a multi-view TechDraw drawing of the result.

### Body and feature tree

```python
body = doc.addObject("PartDesign::Body", "Body")
```

All Part Design features (sketches, pads, pockets, fillets, chamfers) are added to the Body via `body.newObject()`. The Body maintains a linear feature tree — each feature modifies the shape produced by the previous one. The final shape is `body.Shape` (equivalent to `body.Tip.Shape`).

### Sketch attachment

Sketches must be attached to a plane or face via `AttachmentSupport` + `MapMode`.

**Attaching to origin planes.** The Body's Origin provides standard planes at known indices:

```python
# OriginFeatures indices:
#   [0]=X_Axis, [1]=Y_Axis, [2]=Z_Axis,
#   [3]=XY_Plane, [4]=XZ_Plane, [5]=YZ_Plane
sk = body.newObject("Sketcher::SketchObject", "BaseSketch")
sk.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]  # XY_Plane
sk.MapMode = "FlatFace"
```

`MapMode` controls how the sketch aligns to the support. `"FlatFace"` (mode 5) maps the sketch's XY onto the support face, which is the standard mode for Part Design. Other modes exist (see FreeCAD Attachment docs) but `"FlatFace"` is the right choice for Part Design sketches.

**Attaching to a feature face.** After a Pad, Pocket, etc., you often need to sketch on one of its faces (e.g., the top face to add a boss). Face indices like `"Face6"` are **topology-dependent** — they depend on the OCCT kernel's internal face ordering and will shift when the feature tree changes (e.g., inserting a feature before this one). FreeCAD 1.0+ has Topological Naming Protection (TNP) which provides more stable names across edits, but for scripted generation (where the feature tree is created once), use geometric properties to find the right face:

```python
# Find the face with highest Z center-of-mass = top face
shape = pad.Shape
top_face_idx = max(
    range(1, len(shape.Faces) + 1),
    key=lambda i: shape.Faces[i - 1].CenterOfMass.z,
)
sk = body.newObject("Sketcher::SketchObject", "BossSketch")
sk.AttachmentSupport = [(pad, f"Face{top_face_idx}")]
sk.MapMode = "FlatFace"
```

When features have been subtracted (pockets, bores), the original face may have holes. To find it, filter by both position AND area — the base top face with a hole cut in it will still be the largest planar face at the target Z:

```python
candidate_faces = []
for i, f in enumerate(shape.Faces, 1):
    if abs(f.CenterOfMass.z - target_z) < 0.1 and f.Surface.isPlanar():
        candidate_faces.append((i, f))
face_idx = max(candidate_faces, key=lambda x: x[1].Area)[0]
```

### Pad (extrude)

```python
pad = body.newObject("PartDesign::Pad", "BasePad")
pad.Profile = sketch
pad.setExpression("Length", "Params.BaseHeight")
doc.recompute()
```

`setExpression` works without setting `.Length` first — the expression evaluates immediately on `recompute()`.

### Pocket (cut)

```python
pocket = body.newObject("PartDesign::Pocket", "CentralBore")
pocket.Profile = bore_sketch
pocket.Type = 1  # integer enum: 0=Dimension, 1=ThroughAll, 2=ToFirst, 3=ToFace, 4=TwoDimensions
doc.recompute()
```

The `Type` property is an integer enum, not a string. `1` = Through All is the most common for bolt holes and through-bores.

### Fillet and Chamfer

Fillets and chamfers select edges on the previous feature's shape. Like faces, edge indices (`"Edge1"`, etc.) are **topology-dependent** — the OCCT kernel assigns indices based on internal ordering that shifts when the model changes.

For scripted generation, find edges by their geometric properties:

```python
# Example: find the circular edge where the boss cylinder meets the base
shape = previous_feature.Shape
boss_r = BOSS_D / 2
fillet_edges = []
for i, e in enumerate(shape.Edges, 1):
    if not isinstance(e.Curve, Part.Circle):
        continue
    # Match by radius AND position — this uniquely identifies the junction edge
    if abs(e.Curve.Radius - boss_r) < 0.5 and abs(e.Curve.Center.z - BASE_H) < 0.5:
        fillet_edges.append(f"Edge{i}")

fillet = body.newObject("PartDesign::Fillet", "BossFillet")
fillet.Base = (previous_feature, fillet_edges)
fillet.setExpression("Radius", "Params.BossFilletRadius")
```

The `Base` property takes a tuple: `(feature_object, ["Edge1", "Edge2", ...])`. The feature object must be the immediately preceding feature in the tree (the one whose shape contains these edges).

For straight edges, filter by `Part.Line` type and vertex positions:

```python
chamfer_edges = []
for i, e in enumerate(shape.Edges, 1):
    if not isinstance(e.Curve, Part.Line):
        continue
    v0, v1 = e.Vertexes[0].Point, e.Vertexes[1].Point
    # Both endpoints at z=BaseHeight, on the outer rectangular perimeter
    if abs(v0.z - BASE_H) < 0.1 and abs(v1.z - BASE_H) < 0.1:
        on_perimeter = abs(abs(v0.x) - BASE_L / 2) < 0.1 or abs(abs(v0.y) - BASE_W / 2) < 0.1
        if on_perimeter:
            chamfer_edges.append(f"Edge{i}")

chamfer = body.newObject("PartDesign::Chamfer", "BaseChamfer")
chamfer.Base = (previous_feature, chamfer_edges)
chamfer.setExpression("Size", "Params.BaseChamfer")
```

### Parametric principle: relationships live in FreeCAD, not Python

The Python script runs **once** to generate an FCStd file. The FCStd must encode all dimensional relationships internally so they survive editing in the FreeCAD GUI. If you compute a value in Python and assign it to a property, the FCStd stores a scalar constant — there is no link back to the spreadsheet.

```python
# WRONG — the FCStd stores 5.0, not a reference to the spreadsheet.
# Changing Params.BossFilletRadius in the GUI won't update the fillet.
fillet.Radius = float(sheet.get("B10"))

# RIGHT — the FCStd stores an expression "Params.BossFilletRadius".
# Changing the spreadsheet cell updates the fillet on recompute.
fillet.setExpression("Radius", "Params.BossFilletRadius")
```

`setExpression` does not require setting the property first — it evaluates the expression immediately on `doc.recompute()`.

The only place Python-computed values are acceptable is for initial sketch geometry placement. The sketch geometry positions are approximate starting points that get overridden by constraints bound via `setExpression`. For all feature properties (`Length`, `Radius`, `Size`, etc.), use `setExpression` exclusively.

### Multi-view TechDraw for 3D parts

Create multiple `DrawViewPart` objects on one page, each with a different `Direction` vector. `Direction` is the viewing direction (camera look vector), and `XDirection` defines which way is "right" in the projected view.

```python
# Front view (looking along -Y, shows X-Z plane)
front = doc.addObject("TechDraw::DrawViewPart", "FrontView")
front.Source = [body]
front.Direction = App.Vector(0, -1, 0)
front.XDirection = App.Vector(1, 0, 0)

# Right view (looking along +X, shows Y-Z plane)
right = doc.addObject("TechDraw::DrawViewPart", "RightView")
right.Source = [body]
right.Direction = App.Vector(1, 0, 0)
right.XDirection = App.Vector(0, 1, 0)

# Top view (looking along -Z, shows X-Y plane)
top = doc.addObject("TechDraw::DrawViewPart", "TopView")
top.Source = [body]
top.Direction = App.Vector(0, 0, -1)

# Isometric
iso = doc.addObject("TechDraw::DrawViewPart", "IsoView")
iso.Source = [body]
iso.Direction = App.Vector(1, -1, 1)
```

**Direction and XDirection must be perpendicular.** TechDraw builds a projection coordinate system (CS) from these two vectors. When `Direction` is axis-aligned (e.g., `(1, 0, 0)`), FreeCAD's default `XDirection` algorithm may produce a degenerate CS, causing "failed to create projection CS" errors. The fix is to explicitly set `XDirection` to any vector perpendicular to `Direction`. For axis-aligned directions, the safe choices are:

| Direction    | XDirection  | View                |
| ------------ | ----------- | ------------------- |
| `(0, -1, 0)` | `(1, 0, 0)` | Front               |
| `(1, 0, 0)`  | `(0, 1, 0)` | Right side          |
| `(-1, 0, 0)` | `(0, 1, 0)` | Left side           |
| `(0, 0, -1)` | `(1, 0, 0)` | Top (default works) |
| `(0, 0, 1)`  | `(1, 0, 0)` | Bottom              |

For non-axis-aligned directions (like isometric), FreeCAD can compute XDirection automatically — but setting it explicitly never hurts.

**`getVisibleEdges()` returns edges in unscaled model coordinates.** Despite the view having a `Scale` property, the edges from `getVisibleEdges()` use the original model dimensions, not the scaled drawing dimensions. When matching circle edges by radius, compare against the model radius directly: `abs(e.Curve.Radius - bore_r) < tolerance`, not `bore_r * scale`.

**Cylinder projections produce BSplineCurves, not Lines.** When a cylindrical surface (e.g., a boss pad or bore) is projected in TechDraw, the visible silhouette edges are `BSplineCurve` objects, not `Part.Line`. Do not filter edges with `isinstance(e.Curve, Part.Line)` when looking for edges on cylindrical features — match by geometric extent (`_edge_dx`, `_edge_dy`) regardless of curve type. This applies to the top/bottom edges of cylindrical bosses in front/right views, and to any edge that is the silhouette of a curved surface.

See <build_bearing_block_techdraw.py> for a complete multi-view TechDraw example with dimensions.

### Rendering PartDesign bodies to PNG

For rendering Part Design models from multiple camera angles, see <render_multi_angle.py>.

**Body visibility.** A `PartDesign::Body` delegates rendering to its Tip feature — the last feature in the tree. The Body acts as a container; setting `DisplayMode = "Shaded"` on the Body has no visible effect. You must configure the Tip's ViewObject:

```python
for obj in doc.Objects:
    if obj.TypeId == "PartDesign::Body" and hasattr(obj, "Tip") and obj.Tip:
        obj.ViewObject.Visibility = True       # show the body container
        tip_vo = obj.Tip.ViewObject
        tip_vo.Visibility = True               # show the tip feature
        tip_vo.DisplayMode = "Shaded"
        tip_vo.ShapeColor = (0.75, 0.75, 0.80)
        tip_vo.Lighting = "One side"
```

**Rendering documents that contain TechDraw views.** Loading an FCStd with TechDraw `DrawViewPart` objects triggers view recomputation. If any view has a broken projection CS (see above), FreeCAD logs errors that can crash the 3D viewport (`getCameraNode` exception). Before accessing the 3D viewport for rendering, hide all non-3D objects:

```python
_3D_TYPES = {"Part::Feature", "PartDesign::Body"}
for obj in doc.Objects:
    vo = getattr(obj, "ViewObject", None)
    if vo and hasattr(vo, "Visibility"):
        vo.Visibility = obj.TypeId in _3D_TYPES
```

## Visual Debugging

When edge-finding predicates fail or you need to understand the geometry of a TechDraw projection, use the debug renderers to produce annotated images.

### Debug edge renderer

`render_debug_edges.py` draws each visible edge in a TechDraw view with a unique color and labels it with its index, curve type, and dimensions. One PNG per view.

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & sleep 1
DISPLAY=:99 INPUT=/work/model.FCStd OUTDIR=/output /opt/FreeCAD.AppImage freecad render_debug_edges.py
# Produces: FrontView_debug_edges.png, TopView_debug_edges.png, etc.
```

Use this to visually map Edge indices to geometric features before writing `find_unique_edge` / `find_ranked_edge` predicates. Each edge shows its type (Line, BSplineCurve, Circle), dx/dy span, and radius (for circles).

### Debug face renderer

`render_debug_faces.py` colors each face of a Part Design body with a unique color (golden-ratio hue spacing) and logs the face index, surface type, area, and center-of-mass. One isometric PNG output.

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & sleep 1
DISPLAY=:99 INPUT=/work/model.FCStd OUTDIR=/output /opt/FreeCAD.AppImage freecad render_debug_faces.py
# Produces: debug_faces.png + stderr log with Face1..FaceN details
```

Use this to identify which Face index to use for `AttachmentSupport` when sketching on faces, or to verify that face-finding predicates (`max(CenterOfMass.z)`, `max(Area)` among planar faces at target Z) are selecting the right face.

### Visual debug workflow

1. Run the build script to produce the FCStd
2. Run the debug renderer(s) on it
3. Inspect the output PNGs — identify which Edge/Face corresponds to what you need
4. Write your predicate to match that specific Edge/Face by its geometric properties
5. Use `find_unique_edge` (asserts exactly 1 match) or `find_ranked_edge` (deterministic ranking among matches) — never pick arbitrarily from multiple matches

## 3D Modeling with Part Primitives

### Building 3D models

Use `Part` primitives and boolean operations for solid geometry (simpler alternative to Part Design for basic shapes):

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

Use FreeCAD's GUI viewport under Xvfb for offscreen 3D rendering with perspective and lighting.
Runs under the GUI binary (needs OpenGL/Coin for the 3D viewport):

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
DISPLAY=:99 INPUT=/tmp/model.FCStd OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecad render_fcstd.py
```

Key steps in the render script (see <render_fcstd.py>):

1. `init_gui()` — get `QApplication.instance()` (GUI binary initializes it automatically)
2. Open the FCStd, set `Part::Feature` objects to `Shaded` display mode
3. Set perspective camera position and add a directional light
4. `view.fitAll()` + `view.saveImage(path, 800, 600, "Current")` to capture
5. `doc.setClosable(True); App.closeDocument(doc.Name); Gui.getMainWindow().close()` — clean exit

## Export

Example scripts produce FCStd files. Use `export_page.py` to export to DXF, SVG, and PDF.
Both scripts use the GUI binary (TechDraw requires the Qt event loop):

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & sleep 1
DISPLAY=:99 OUTDIR=. /opt/FreeCAD.AppImage freecad parametric_sketch.py   # → bracket.FCStd
DISPLAY=:99 INPUT=bracket.FCStd OUTDIR=. /opt/FreeCAD.AppImage freecad export_page.py  # → bracket.{dxf,svg,pdf}
```

Arguments are passed via env vars (`INPUT`, `OUTDIR`) because the `freecad` binary also treats CLI args as files to open. See <export_page.py>. Output filenames derive from the input FCStd stem.

**DXF → PNG rendering:** `python3 render_dxf.py output.dxf output.png`. See <render_dxf.py>.

### Format comparison

| Format | API                                       | Strengths                                                       | Limitations                                           |
| ------ | ----------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------- |
| DXF    | `TechDraw.writeDXFPage(page, path)`       | CAD-compatible, editable in other CAD tools                     | R12/R14 only, font rendering depends on ezdxf for PNG |
| SVG    | `TechDrawGui.exportPageAsSvg(page, path)` | Vector, viewable in browsers, preserves template/fonts natively | Hatch patterns not exported (Qt SVG limitation)       |
| PDF    | `TechDrawGui.exportPageAsPdf(page, path)` | Print-ready, universal viewer support                           | Largest file size                                     |

### View computation: waiting for TechDraw HLR

TechDraw runs Hidden Line Removal (HLR) and face extraction asynchronously via
`QtConcurrent` threads. After `doc.recompute()`, the HLR thread starts but
`recompute()` returns immediately — the view's geometry is not yet available.

**Why `processEvents()` is required:** The `QFutureWatcher::finished` Qt signal
dispatches HLR completion back to the main thread. Without calling
`qapp.processEvents()`, this signal is never delivered and `getVisibleEdges()`
stays empty forever. A bare `time.sleep()` will NOT work.

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

This replaces the old fixed-duration `pump(5)` + `pump(2)` pattern (7 seconds
of sleeping regardless of actual computation time) with polling that completes
as soon as the HLR thread finishes — typically under 2 seconds.

**What's NOT available from Python** (C++ only, not exposed in FreeCAD 1.1.0
Python bindings): `waitingForHlr()`, `waitingForFaces()`, `waitingForResult()`.
These are internal `DrawViewPart` state flags. The `getVisibleEdges()` check is
the best Python-accessible readiness indicator.

**For 3D viewport rendering** (not TechDraw): There is no edge-based readiness
indicator. Use `pump()` with conservative fixed durations and `processEvents()`.

The FCStd caches computed view edges when saved during a GUI session. Reloading
a previously-cached file shows edges immediately. But freshly created views
always require event pumping.

## Gotchas

| Issue                                | Fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TechDraw 0 edges                     | Poll `getVisibleEdges()` with `processEvents()` in a loop (see `wait_for_view()` above)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Open Wire / loose edges              | Use Faces only — open topology crashes HLR projector                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Multiple views lose positions        | Single compound, single view                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| FreeCAD import (system pkg)          | `sys.path.insert(0, '/usr/lib/freecad-python3/lib')` — AppImage sets up paths automatically via AppRun                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `getConstruction` typo               | Correct API name                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Property "mm" suffix                 | `re.sub(r'\s*mm$', '', str(val))` before numeric use                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Under-constrained sketch             | Add constraints until `FullyConstrained == True`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Removing geometry                    | Shifts indices — use `toggleConstruction` instead                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| DXF Y convention                     | CAD Y-up: sketch Y=0 (e.g. window wall) at bottom of render                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Annotation placement                 | Absolute page coords; use bounding box center math to convert from sketch coords                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Dim text overlaps at center          | `DrawViewDimension` defaults `X=0, Y=0` (view center) — must set `dim.X`/`dim.Y`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Dim text on dimension line           | Offset `dim.X`/`dim.Y` so text clears the dimension line, especially for vertical dims where horizontal text can sit on the line                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Duplicate dims in DXF                | `writeDXFPage` may emit each dimension twice — known FreeCAD export artifact                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Dim "larger than page" warns         | Dimension geometry extends beyond template bounds — cosmetic, does not break DXF                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Tangent + Coincident overlap         | `Tangent` with point refs (e.g., `line, 2, arc, 1`) implies coincidence — adding a separate `Coincident` at the same points over-constrains the sketch                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Angle constraint on one line         | `Constraint("Angle", line_idx, radians)` constrains angle from X axis. For two-line angles, both lines must share a point                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Angle expression needs `deg` unit    | `setExpression("Constraints[N]", "Params.Angle * 1 deg")` — raw radian values without unit annotation are treated as dimensionless, producing wrong angles                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| TechDraw edge Y is inverted          | `getVisibleEdges()` returns view-local coords with Y inverted (edge_y = cy - sketch_y). Match edges by geometric properties (slope, radius) not raw coordinates                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `print()` invisible in freecadcmd    | freecadcmd may buffer stdout; use `print(msg, file=sys.stderr, flush=True)` for debug output captured in subprocess stderr                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| PDF font subset names                | PDF exports embed fonts with non-deterministic subset prefixes (e.g., `QNAAAA+DejaVuSans`). Golden files must come from the same environment (RBE) as tests                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Annotation Y direction               | Page Y increases **upward** (Y=0 is bottom). Use `view.Y + (sketch_y - cy) * scale`, NOT `view.Y - ...`. The minus formula in old docs is wrong                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Quantity unit mismatch               | `view.X`, `view.Y`, `view.Scale` return FreeCAD Quantity objects. Cast to `float()` before mixing with plain Python floats in arithmetic                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Unicode in DXF                       | Unicode chars (e.g., `\u00b0` degree symbol) corrupt in DXF export. Use ASCII alternatives (`"60 deg"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Radius dims as text annotations      | Do NOT manually write "R12" text annotations. Use `DrawViewDimension` with `Type="Radius"` and `References2D=[(view, "EdgeN")]` for proper auto-computed callouts                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Redundant constraints confuse solver | Adding `Parallel` between two `Horizontal` lines is redundant and can cause `FullyConstrained=False` despite correct DOF count                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Open wires for geometric features    | Features like tabs/gussets must be closed faces integrated into the profile contour, not separate open wires rendered as thin strips                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Edge finding with `getVisibleEdges`  | Match edges by geometric properties (length, slope, position), not index. Use `isinstance(e.Curve, Part.Line)` for straight edges, `Part.Circle` for arcs. Filter horizontal/vertical via `_edge_dx < tol` / `_edge_dy < tol`. Pick by extremes (`min`/`max` on position) when multiple candidates exist                                                                                                                                                                                                                                                                                                  |
| Dim text invisible at large scale    | At `view.Scale=1.0` with large geometry (e.g. 4000mm room), dimension text (~3.5mm) is invisible in DXF→PNG renders. Dimensions are still present in the DXF/SVG/PDF — verify via SVG inspection or PDF viewer. Use a smaller `view.Scale` if visual readability matters                                                                                                                                                                                                                                                                                                                                  |
| `DiffuseColor` alpha convention      | FreeCAD's `DiffuseColor` tuples are `(R, G, B, A)` floats 0.0–1.0 where **`A=1.0` = opaque** and `A=0.0` = transparent. This is the opposite of CSS/WebGL convention. Using `A=0.0` renders the object invisible (blank white). Internally (`Base/Color.cpp`): `transparency = 1.0 - alpha`. Confirmed in FreeCAD's `ColorPerFaceTest` (`src/Mod/Part/Gui/Tests/`)                                                                                                                                                                                                                                        |
| `DiffuseColor` list length           | The list length MUST exactly equal `len(Shape.Faces)`. If it doesn't match the Coin3D face count, per-face coloring **silently does nothing** — no error, no effect, the shape keeps its previous single color. The Coin3D binding check is in `ViewProviderPartExt.cpp`: `if (size > 1 && size == faceset->partIndex.getNum())`                                                                                                                                                                                                                                                                          |
| `DiffuseColor` on PartDesign bodies  | Setting `DiffuseColor` on a `PartDesign::Body`'s Tip ViewObject doesn't work — the Body overrides child view properties. Workaround: create a temporary `Part::Feature`, copy the body's shape into it, then set `DiffuseColor` on the standalone feature. Call `doc.recompute()` before setting colors, then `ViewObject.update()` after                                                                                                                                                                                                                                                                 |
| Cylinder projections are BSplines    | Cylindrical surfaces (boss, bore) project as `BSplineCurve` in TechDraw, not `Part.Line`. Don't filter with `isinstance(e.Curve, Part.Line)` for edges on cylindrical features — match by geometric extent (`_edge_dx`, `_edge_dy`) regardless of curve type                                                                                                                                                                                                                                                                                                                                              |
| Multiple edges at same dimension     | A chamfered rectangular base has two full-width horizontal edges (top and bottom). Don't assume a dimension predicate (e.g., `dx=BaseLength`) returns a unique edge. Use `find_ranked_edge` with explicit ranking (`max(y)` for bottom-most in TechDraw's inverted Y) instead of picking arbitrarily                                                                                                                                                                                                                                                                                                      |
| TechDraw Y axis is inverted          | In TechDraw `getVisibleEdges()`, the Y axis points downward (higher Y = lower position in the model). The base bottom (model z=0) has higher Y than the boss top (model z=30). Ranking by `max(y)` gives the bottom edge, `min(y)` gives the top edge                                                                                                                                                                                                                                                                                                                                                     |
| PDF non-determinism sources          | FreeCAD PDFs have 4 sources of non-determinism: (1) `/Info` dict timestamps, (2) XMP metadata with ISO dates and UUIDs, (3) font subset prefixes (`QNAAAA+`), (4) `FlateDecode` zlib compression (varies between runs for identical input). Golden comparison must decompress content and normalize all 4                                                                                                                                                                                                                                                                                                 |
| SVG element ordering                 | Qt emits SVG child elements (glyph paths) in non-deterministic order within groups. Multi-view TechDraw exports have enough elements that this becomes visible. Golden comparison must sort child elements before diffing                                                                                                                                                                                                                                                                                                                                                                                 |
| References3D face-to-face gap        | `DrawViewDimension` with `MeasureType="True"` works for cylinder diameter/radius and edge lengths, but NOT for face-to-face distance. `Measurement.planePlaneDistance()` exists but `getTrueDimValue()` doesn't call it for the `TwoPlanes` case (FreeCAD 1.1.0). Use projected-edge DistanceY as workaround                                                                                                                                                                                                                                                                                              |
| Sketcher Symmetric: 5-arg vs 6-arg   | The 5-arg `Symmetric(g1, p1, g2, p2, axisGeo)` creates **line symmetry** (mirror about an axis). The 6-arg `Symmetric(g1, p1, g2, p2, geoId, ptId)` creates **point symmetry** (180° rotation about a point). Using 6 args with `(-1, 1)` or `(-2, 1)` mirrors about the **origin point**, not an axis — both resolve to the same point (0,0). For axis mirroring, use 5 args: `Symmetric(..., -1)` = mirror about X axis (HAxis, flips Y), `Symmetric(..., -2)` = mirror about Y axis (VAxis, flips X). Source: `GeoEnum.h` (`HAxis=-1`, `VAxis=-2`), `Sketch.cpp:2425` (dispatch on `ThirdPos != none`) |

### Visual inspection checklist

After generating a TechDraw PNG, visually inspect the output for:

- **Text overlapping geometry** — dimension labels sitting on top of edges or other labels
- **Text on dimension lines** — especially vertical dimensions where horizontal text can land directly on the arrow line; offset text away from the line
- **Extension line overshoot** — lines extending well beyond the geometry they reference
- **Cramped or cut-off labels** — text too close to drawing edges or clipped by the viewport
