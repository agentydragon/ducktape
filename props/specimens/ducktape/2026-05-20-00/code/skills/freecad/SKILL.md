---
name: freecad-sketcher
description: Use this skill for parametric 2D/3D technical drawings using FreeCAD Sketcher and TechDraw. Triggers when the user wants constrained parametric floor plans, mechanical sketches, layout diagrams, or any drawing where dimensions drive geometry. Also use when the user mentions FreeCAD, .FCStd files, Sketcher, TechDraw, parametric CAD, or technical drawings. Use this skill even for seemingly simple 2D layouts — the parametric constraint approach prevents coordinate drift and makes edits safe. Always read this skill before writing any FreeCAD scripting code.
---

# FreeCAD Sketcher + TechDraw Skill

## Philosophy

The FCStd is the artifact; images are derived previews. Work iteratively: open, edit, save,
export, visually check, repeat. Every dimension is a constraint.

**Parametric-first**: Spreadsheet parameters are the single source of truth (SSOT). All
sketch constraints must be bound to spreadsheet cells via `setExpression()`. All TechDraw
dimensions must reference projected entities (`References2D`) so they auto-update when
parameters change. Never use hardcoded coordinates, one-shot Python variables, or
point-based `makeDistanceDim`.

## Critical Pitfalls

These are the most common silent failures. Read before writing any code:

- **`setExpression`, not direct assignment.** `fillet.Radius = float(...)` stores a scalar —
  the FCStd has no link back to the spreadsheet. Use `fillet.setExpression("Radius", "Params.X")`.
- **Never use `makeDistanceDim`.** It creates non-parametric point-based dimensions. Always
  use `DrawViewDimension` with `References2D = [(view, "EdgeN")]`.
- **TechDraw HLR requires Xvfb.** `QT_QPA_PLATFORM=offscreen` does NOT provide the OpenGL
  context the HLR thread needs. Use `xvfb-run -a` or a manual `Xvfb :99` display.
- **TechDraw HLR is async.** After `doc.recompute()`, call `wait_for_view()` — it polls
  `getVisibleEdges()` with `processEvents()`. A bare `time.sleep()` will never deliver the
  Qt signal and `getVisibleEdges()` will stay empty forever.
- **Never hardcode face/edge indices.** `"Face6"` or `"Edge3"` shift when the model changes.
  Always find faces/edges by geometric properties (`CenterOfMass.z`, `Radius`, `Area`,
  vertex positions).
- **`Tangent` with point refs implies coincidence.** `Tangent(line, 2, arc, 1)` means the
  endpoints are already joined — adding a separate `Coincident` at the same points
  over-constrains the sketch.
- **Single compound → single TechDraw view.** Multiple `Part::Feature` objects lose their
  relative positions (each view centers its own bounding box independently).
- **Closed faces only in TechDraw.** Open wires or loose edges crash the HLR projector
  with `NCollection_Array1::Create`.
- **Angle expressions need `deg` unit.** `sk.setExpression(f"Constraints[{i}]", "Params.Angle * 1 deg")` —
  raw values without `* 1 deg` are treated as dimensionless, producing wrong angles.
- **`dim.X`/`dim.Y` must be set.** `DrawViewDimension` defaults to `(0, 0)` — all dimension
  text overlaps without explicit placement.

## Setup

**Target version: FreeCAD 1.1.0.** The API surface changed between 0.21 and 1.0 (Topological
Naming Protection, PySide6 migration). Pin to 1.1.0 to avoid compatibility surprises.

```bash
wget -q "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.0/FreeCAD_1.1.0-Linux-x86_64-py311.AppImage" -O /opt/FreeCAD.AppImage
chmod +x /opt/FreeCAD.AppImage
apt-get install -y xvfb          # required for TechDraw HLR
pip install ezdxf[draw] --break-system-packages  # for DXF→PNG rendering
```

Alternatively: `apt-get install -y freecad-python3 xvfb` with
`sys.path.insert(0, '/usr/lib/freecad-python3/lib')`.

## Script Execution Model

For iterative work (exploring geometry, debugging dimensions), use Interactive Mode instead
of standalone scripts — see the Interactive Mode section below.

FreeCAD has two binaries with very different lifecycle models:

| Binary       | Event loop    | Use for                                                             |
| ------------ | ------------- | ------------------------------------------------------------------- |
| `freecadcmd` | No `exec()`   | TechDraw + geometry scripts under `xvfb-run` (module-level pattern) |
| `freecad`    | `exec()` runs | 3D rendering (Coin3D/OpenGL), QTimer.singleShot pattern             |

### What needs Xvfb vs offscreen

| Operation                                 | Needs Xvfb? | Notes                                                    |
| ----------------------------------------- | ----------- | -------------------------------------------------------- |
| TechDraw HLR / DXF / SVG / PDF export     | **Yes**     | HLR thread needs an OpenGL context; Qt offscreen doesn't |
| 3D render via Coin3D/pivy                 | **Yes**     | Use `freecad` binary with direct Xvfb (not `xvfb-run`)   |
| Part Design solid geometry (no rendering) | No          | Use `freecadcmd` with `QT_QPA_PLATFORM=offscreen`        |

**TechDraw invocation (freecadcmd + xvfb-run):**

```bash
OUTDIR=/tmp/out xvfb-run -a /opt/FreeCAD.AppImage freecadcmd parametric_sketch.py
INPUT=/tmp/bracket.FCStd OUTDIR=/tmp/out xvfb-run -a /opt/FreeCAD.AppImage freecadcmd export_page.py
```

Scripts use module-level code, call `FreeCADGui.showMainWindow()`, and end with `os._exit(0)` to
bypass the Qt6 TLS crash — see <debug/qt_shutdown_segfault.md>.

**3D rendering (freecad GUI binary + direct Xvfb):** Do NOT use `xvfb-run` with the `freecad`
binary — it can hang. Start Xvfb manually:

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
DISPLAY=:99 INPUT=/tmp/model.FCStd OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecad render_fcstd.py
```

**Headless geometry only (no rendering):**

```bash
QT_QPA_PLATFORM=offscreen OUTDIR=/tmp/out /opt/FreeCAD.AppImage freecadcmd build_cube.py
```

### Fonts

FreeCAD bundles **osifont** in `<ResourceDir>/Mod/TechDraw/Resources/fonts/`. Register it
with fontconfig for deterministic TechDraw exports across machines:

```bash
ln -sf /opt/squashfs-root/usr/Mod/TechDraw/Resources/fonts /usr/local/share/fonts/techdraw
fc-cache -f
```

## Sketcher

One `Sketcher::SketchObject` holds all geometry and constraints. For parametric designs, use a
`Spreadsheet::Sheet` to drive constraint values via `setExpression()`. For simple examples,
Python variables as constraints are fine — but they won't survive GUI editing.

@sketcher.md

## Part Features

TechDraw projects `Part::Feature` shapes via HLR. Shape topology rules:

- `Part.Face` from closed wire: works
- `Part.Compound` of Faces: works
- Open `Part.Wire` or loose-edge Compound: crashes with `NCollection_Array1::Create`

All geometry must be closed faces — model walls as closed polygons tracing inner and outer
outlines (shell approach). See <examples/compound/build.py> for an L-shaped wall shell.

**Single compound, single view.** Put ALL faces in one `Part.Compound` → one `Part::Feature`
→ one `TechDraw::DrawViewPart`. Multiple features with multiple views lose relative positions
because TechDraw centers each view's bounding box independently.

## TechDraw

`DrawPage` + `DrawSVGTemplate`. Dimensions must reference projected edges via `References2D`,
not hardcoded coordinates. The `wait_for_view()` pattern (polling `getVisibleEdges()` with
`processEvents()`) is required after every `doc.recompute()`.

@techdraw.md

## Part Design

The Part Design workbench (`PartDesign::Body`) is the standard approach for solid 3D modeling.
Feature tree: Sketch → Pad → Pocket → Fillet/Chamfer. Always use `setExpression` for all
feature properties to keep the FCStd parametric.

@partdesign.md

## 3D Modeling with Part Primitives

Use `Part` primitives and boolean operations for simple solid geometry (alternative to Part
Design for basic shapes):

```python
import FreeCAD
import Part

cube = Part.makeBox(20, 20, 20, FreeCAD.Vector(-10, -10, -10))
cylinder = Part.makeCylinder(5, 22, FreeCAD.Vector(0, 0, -11), FreeCAD.Vector(0, 0, 1))
result = cube.cut(cylinder)

feat = doc.addObject("Part::Feature", "CubeWithHole")
feat.Shape = result
```

See <examples/cube_with_hole/build.py> for a complete example.

## Visual Debugging

When edge-finding predicates fail or you need to understand TechDraw projection geometry,
use the debug renderers.

`render_debug_edges.py` draws each visible edge with a unique color and labels it with its
index, curve type, and dimensions. Run under Xvfb with the `freecad` binary:

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & sleep 1
DISPLAY=:99 INPUT=/work/model.FCStd OUTDIR=/output /opt/FreeCAD.AppImage freecad render_debug_edges.py
# Produces: FrontView_debug_edges.png, TopView_debug_edges.png, etc.
```

`render_debug_faces.py` colors each face of a Part Design body with a unique color and logs
face index, surface type, area, and center-of-mass. Same invocation, produces `debug_faces.png`.

**Workflow:** produce FCStd → run debug renderer → inspect output PNGs → identify which
Edge/Face to target → write geometric predicate → use `find_unique_edge` (asserts exactly 1
match) or `find_ranked_edge` (deterministic ranking among multiple matches).

## Export

Example scripts produce FCStd files. Use `export_page.py` to export to DXF, SVG, and PDF.
Arguments are passed via env vars (`INPUT`, `OUTDIR`) because the `freecad` binary treats CLI
args as files to open:

```bash
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & sleep 1
DISPLAY=:99 OUTDIR=. /opt/FreeCAD.AppImage freecad parametric_sketch.py   # → bracket.FCStd
DISPLAY=:99 INPUT=bracket.FCStd OUTDIR=. /opt/FreeCAD.AppImage freecad export_page.py  # → bracket.{dxf,svg,pdf}
```

**DXF → PNG:** `python3 render_dxf.py output.dxf output.png`. See <examples/render_dxf.py>.

| Format | API                                       | Notes                                                                 |
| ------ | ----------------------------------------- | --------------------------------------------------------------------- |
| DXF    | `TechDraw.writeDXFPage(page, path)`       | CAD-compatible; R12/R14 only; font rendering needs ezdxf for PNG      |
| SVG    | `TechDrawGui.exportPageAsSvg(page, path)` | Vector; browser-viewable; hatch patterns not exported (Qt limitation) |
| PDF    | `TechDrawGui.exportPageAsPdf(page, path)` | Print-ready; largest file size                                        |

## Gotchas

@gotchas.md

@interactive.md
