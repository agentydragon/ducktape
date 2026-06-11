# Baseplate Evaluation Checklist

Open the agent's FCStd in the FreeCAD container and inspect it (run
FreeCAD Python to check geometry, constraints, parametric behavior).
Review any rendered images/SVGs the agent produced in the workspace.

## Script execution

- [ ] Script ran without errors and produced `baseplate.FCStd`

## Sketch quality

- [ ] Sketch is fully constrained (`fully_constrained: true` in inspection)
- [ ] Dimensional constraints are bound to spreadsheet via expressions
      (`expression_count` > 0, expressions reference spreadsheet aliases)

## Spreadsheet

- [ ] Spreadsheet exists with named aliases for all 7 spec dimensions
      (plate width/height, corner radius, hole diameter, hole inset,
      slot width, slot height)
- [ ] Default values match spec (200, 120, 10, 8, 20, 40, 15)

## Geometry (from `inspection.json` features + rendered images)

- [ ] Outer profile is a rounded rectangle with corner fillets (~10mm radius)
- [ ] 4 mounting holes at inset corners (~4mm radius circles in `circular_edges`)
- [ ] Central slot present and centered on the plate
- [ ] Bounding box approximately 200 x 120

## Parametric behavior

To test: change the plate-width spreadsheet cell to 250 and recompute. Check:

- [ ] Bounding box width updates to ~250
- [ ] Height remains unchanged (~120)
- [ ] Right-side holes shift rightward
- [ ] Slot remains centered

_(The inspector dumps all geometry; the judge performs this test manually
or the parametric check can be scripted separately.)_

## TechDraw

- [ ] Page exists with a top-down view
- [ ] View has visible edges (> 0)
- [ ] At least 4 entity-referenced dimensions (`has_entity_refs: true`)
- [ ] Dimension labels readable and not overlapping geometry (check SVG)

## Visual inspection (from `top_view.svg` and `3d_render.png`)

- [ ] Drawing looks like a baseplate — rounded rect with holes and slot
- [ ] Dimension text is legible and correctly placed
- [ ] No overlapping labels or extension lines through geometry

## Problems found

_Describe any issues here._

## Score

| Criterion         | Weight | Score (0-2) | Notes |
| ----------------- | ------ | ----------- | ----- |
| Runs correctly    | 1x     |             |       |
| Fully constrained | 2x     |             |       |
| Correct geometry  | 2x     |             |       |
| Parametric        | 2x     |             |       |
| TechDraw quality  | 1x     |             |       |
| Visual quality    | 1x     |             |       |

**Total: \_\_ / 18**

Scoring: 0 = missing/broken, 1 = partially correct, 2 = fully correct.
