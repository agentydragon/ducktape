# FreeCAD Gotchas

| Issue                                | Fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FreeCAD import (system pkg)          | `sys.path.insert(0, '/usr/lib/freecad-python3/lib')` — AppImage sets up paths automatically via AppRun                                                                                                                                                                                                                                                                                                                                                              |
| Property "mm" suffix                 | `re.sub(r'\s*mm$', '', str(val))` before numeric use                                                                                                                                                                                                                                                                                                                                                                                                                |
| Under-constrained sketch             | Add constraints until `FullyConstrained == True`                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Removing geometry                    | Shifts indices — use `toggleConstruction` instead                                                                                                                                                                                                                                                                                                                                                                                                                   |
| DXF Y convention                     | CAD Y-up: sketch Y=0 (e.g. window wall) at bottom of render                                                                                                                                                                                                                                                                                                                                                                                                         |
| Annotation placement                 | Absolute page coords; use bounding box center math to convert from sketch coords                                                                                                                                                                                                                                                                                                                                                                                    |
| Dim text on dimension line           | Offset `dim.X`/`dim.Y` so text clears the dimension line, especially for vertical dims where horizontal text can sit on the line                                                                                                                                                                                                                                                                                                                                    |
| Duplicate dims in DXF                | `writeDXFPage` may emit each dimension twice — known FreeCAD export artifact                                                                                                                                                                                                                                                                                                                                                                                        |
| Dim "larger than page" warns         | Dimension geometry extends beyond template bounds — cosmetic, does not break DXF                                                                                                                                                                                                                                                                                                                                                                                    |
| TechDraw edge Y is inverted          | `getVisibleEdges()` returns view-local coords with Y inverted (edge_y = cy - sketch_y). Match edges by geometric properties (slope, radius) not raw coordinates                                                                                                                                                                                                                                                                                                     |
| `print()` invisible in freecadcmd    | freecadcmd may buffer stdout; use `print(msg, file=sys.stderr, flush=True)` for debug output captured in subprocess stderr                                                                                                                                                                                                                                                                                                                                          |
| Radius dims as text annotations      | Do NOT manually write "R12" text annotations. Use `DrawViewDimension` with `Type="Radius"` and `References2D=[(view, "EdgeN")]` for proper auto-computed callouts                                                                                                                                                                                                                                                                                                   |
| Redundant constraints confuse solver | Adding `Parallel` between two `Horizontal` lines is redundant and can cause `FullyConstrained=False` despite correct DOF count                                                                                                                                                                                                                                                                                                                                      |
| Open wires for geometric features    | Features like tabs/gussets must be closed faces integrated into the profile contour, not separate open wires rendered as thin strips                                                                                                                                                                                                                                                                                                                                |
| Edge finding with `getVisibleEdges`  | Match edges by geometric properties (length, slope, position), not index. Use `isinstance(e.Curve, Part.Line)` for straight edges, `Part.Circle` for arcs. Filter horizontal/vertical via `_edge_dx < tol` / `_edge_dy < tol`. Pick by extremes (`min`/`max` on position) when multiple candidates exist                                                                                                                                                            |
| Dim text invisible at large scale    | At `view.Scale=1.0` with large geometry (e.g. 4000mm room), dimension text (~3.5mm) is invisible in DXF→PNG renders. Dimensions are still present in the DXF/SVG/PDF — verify via SVG inspection or PDF viewer. Use a smaller `view.Scale` if visual readability matters                                                                                                                                                                                            |
| `DiffuseColor` alpha convention      | FreeCAD's `DiffuseColor` tuples are `(R, G, B, A)` floats 0.0–1.0 where **`A=1.0` = opaque** and `A=0.0` = transparent. This is the opposite of CSS/WebGL convention. Using `A=0.0` renders the object invisible. Internally: `transparency = 1.0 - alpha`                                                                                                                                                                                                          |
| `DiffuseColor` list length           | The list length MUST exactly equal `len(Shape.Faces)`. If it doesn't match the Coin3D face count, per-face coloring **silently does nothing** — no error, no effect                                                                                                                                                                                                                                                                                                 |
| `DiffuseColor` on PartDesign bodies  | Setting `DiffuseColor` on a `PartDesign::Body`'s Tip ViewObject doesn't work — the Body overrides child view properties. Workaround: create a temporary `Part::Feature`, copy the body's shape into it, then set `DiffuseColor` on the standalone feature                                                                                                                                                                                                           |
| Multiple edges at same dimension     | A chamfered rectangular base has two full-width horizontal edges (top and bottom). Don't assume a dimension predicate returns a unique edge. Use `find_ranked_edge` with explicit ranking (`max(y)` for bottom-most in TechDraw's inverted Y)                                                                                                                                                                                                                       |
| TechDraw Y axis is inverted          | In TechDraw `getVisibleEdges()`, Y points downward (higher Y = lower position in the model). The base bottom (model z=0) has higher Y than the boss top. Ranking by `max(y)` gives the bottom edge, `min(y)` the top edge                                                                                                                                                                                                                                           |
| `qapp.quit()` blocks on save dialog  | After modifying a document with `doc.addObject(...)` / `page.addView(...)`, calling `qapp.quit()` from a `QTimer` callback triggers FreeCAD's close event, which shows a "save changes?" dialog in GUI mode — blocking indefinitely in headless runs. Fix: close all open documents before quitting: `for name in list(FreeCAD.listDocuments().keys()): FreeCAD.closeDocument(name)`. The `run_gui_script()` helper in `freecad_helpers.py` does this automatically |
| Wayland breaks Xvfb rendering        | On Wayland desktops, FreeCAD may connect to Wayland instead of the Xvfb X11 display. Unset `WAYLAND_DISPLAY` and set `QT_QPA_PLATFORM=xcb` before launching FreeCAD under Xvfb                                                                                                                                                                                                                                                                                      |

## Sanity-checking view directions

After creating a `DrawViewPart`, verify the visible edges match what you expect for that
viewing direction. Common mistake: `Direction=(0,0,-1)` looks at the bottom of the part, not
the top. `Direction=(0,0,1)` looks down at the top.

Log the edge count and types, then cross-check against the part's geometry:

```python
vis = view.getVisibleEdges()
circles = [e for e in vis if isinstance(e.Curve, Part.Circle)]
log(f"{view.Name}: {len(vis)} edges, {len(circles)} circles")
# If a top view of a part with a cylindrical boss shows 0 circles besides
# through-holes, the direction is probably inverted.
```

For each view, mentally enumerate which features should be visible from that direction and
verify the edge list matches. Missing expected features = wrong direction or occluded geometry.

## Dimension label positioning

**The origin for `dim.X`/`dim.Y` depends on the dimension type:**

- **Linear dims** (`DistanceX`, `DistanceY`, `Distance`): offsets from the **view center**.
- **Diameter/Radius dims**: offsets from the **circle center**. `dim.Y = 0` gives a horizontal
  leader; `dim.X > 0` places the label to the right of the circle.

**Do not hardcode label positions from model dimensions.** The mapping from model space to
TechDraw view space depends on `Direction`, `XDirection`, and `Scale`. For linear dims, compute
positions from the actual projected edge/vertex coordinates returned by `getVisibleEdges()` /
`getVertexBySelection()`.

## Visual inspection checklist

After generating a TechDraw export, **always render the PDF and visually inspect** before
updating golden files or declaring success. Also check FreeCAD stderr for warnings:

- `DVD::getDimValue - <name> - 2D references are corrupt` — dimension has invalid references,
  will render with tiny/zero-length leader lines. Fix the `References2D`.
- `True dimension has no 3D References` — `References3D` didn't persist after recompute.
  Use `References2D` with projected edges instead.

Visual checks:

- **Text overlapping geometry** — dimension labels sitting on top of edges or other labels
- **Text on dimension lines** — especially vertical dimensions where horizontal text can land directly on the arrow line; offset text away from the line
- **Extension line overshoot** — lines extending well beyond the geometry they reference
- **Cramped or cut-off labels** — text too close to drawing edges or clipped by the viewport
- **Tiny leader arrows** — dimension reads the correct value but the arrows span near-zero distance, meaning the 2D reference doesn't anchor to visible geometry
- **Dimension on wrong view** — place dimensions on views where the measured feature has a clear projected outline. Check FreeCAD stderr for `2D references are corrupt` warnings
- **Diagonal leader lines** — diameter/radius leaders should be horizontal or at a clean angle. If diagonal, the `dim.Y` doesn't match the circle center's projected y-coordinate
