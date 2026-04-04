# FreeCAD Skill TODOs

## Rendering pipeline

- [ ] Fix DXF rendering quality: corner lines extend beyond intersection points. Investigate whether `parametric_rect.py` produces a correctly constrained sketch or if the TechDraw projection is introducing artifacts.
- [ ] Rotate vertical dimension text 90deg once the ezdxf DXF->PNG renderer supports rotated TEXT entities in dimension blocks. Currently it ignores rotation, so we keep text horizontal and offset it right of the dimension line.

## Export formats

- [ ] Consider making SVG the primary intermediate format for PNG rendering (avoids ezdxf font discovery issues).

## FreeCAD scripting

- [ ] Use `more_itertools.one()` for TechDraw page lookup in `export_page.py` (requires adding `more-itertools` to FreeCAD container image).
- [ ] Entity-referenced dimensions: `DrawViewDimension.References2D = [(view, 'Edge0')]` should allow dimensions that reference specific projected edges and auto-update when sketch changes. Current working approach uses `makeDistanceDim` with computed points — functional but not entity-bound.
- [ ] Annotation anchoring: annotations are absolute-positioned on the page. Investigate if TechDraw has leaders/balloons that anchor to view geometry.
- [ ] DXF layer styling: set line colors/weights per DXF layer so `ezdxf draw` renders with visual hierarchy (walls thick/dark, furniture light).

## 3D

- [ ] Raytraced renders: Coin3D shading works but is basic. Investigate FreeCAD Render workbench for POV-Ray/LuxRender output.
- [ ] Multi-view drawings: front/side/top views on one TechDraw page for 3D objects.
- [ ] Assemblies: multi-part models with positioning.

## Docker test image

- [ ] Upgrade FreeCAD from 0.21.2 to 1.1 (latest stable, released 2026-03-25). The 1.0 release included a major PartDesign rework that likely fixes the side-face pocket failures we hit. Update `Dockerfile.test` PPA source or switch to the official FreeCAD PPA/snap. This unblocks the slot, rib, and external geometry features for the bracket example.

## PartDesign bracket example

- [ ] Add PartDesign::Pocket on a side face (slot in wall). FreeCAD 0.21 Pocket fails on Y/X-normal faces with "shape is invalid" — face normals are inverted, causing the cut direction to be wrong. Likely fixed in FreeCAD 1.0+ — try after upgrading the test image.
- [ ] Add reinforcement rib (Sketch on side face → Pad). Same face-normal issue blocks this.
- [ ] Add external geometry references (`addExternal`) once side-face sketches work.
- [ ] Fix existing Docker tests (`test_render_3d`, `test_container_primitives`, `test_export_formats`) that use the old `load_image` API (renamed to `load_oci_image` with `OciImage` dataclass).
- [ ] Investigate `crane push daemon://` failures on RBE — all Docker-based freecad tests fail with crane unable to push to local Docker daemon on BuildBuddy workers.
