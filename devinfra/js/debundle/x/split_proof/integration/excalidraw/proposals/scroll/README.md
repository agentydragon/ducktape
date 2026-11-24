# Scroll dependency selectors for the pinned editor

These three files contain additional `source_matches` for the v0.18.1
editor. Append each list to the corresponding owner in a scratch copy of
the full editor spec, creating the file if necessary. In the editor import
priority, insert `packages/excalidraw/scene/shape` and
`packages/utils/geometry/shape`, in that order, immediately before
`packages/excalidraw/scene/scroll`. This keeps the ordinary Rust gate fixture
separate from the proposal.

Start with the 225-binding selection from devel at `d1778691`. Keep its claims
and select all ten names in these files plus `isOutsideViewPort` from the
existing scroll spec. The [resolver-only export](https://app.buildbuddy.io/invocation/2bfcc31d-e893-4812-bd72-f35299105edc)
has 236 claimed units among 1,787, with source SHA-256
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`.

| Pinned upstream source                                                                                                           | Additional editor units                                                                                                                          |
| -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| [utils/geometry/shape.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/utils/geometry/shape.ts#L194)           | 1132: `getCurvePathOps`                                                                                                                          |
| [excalidraw/scene/Shape.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/scene/Shape.ts#L30)        | 1237–1240: `getDashArrayDashed`, `getDashArrayDotted`, `adjustRoughness`, `generateRoughOptions`                                                 |
| [excalidraw/element/bounds.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/element/bounds.ts#L372) | 1253–1256: `getBezierValueForT`, `solveQuadratic`, `getCubicBezierCurveBound`, `getMinMaxXYFromCurvePathOps`; 1262: `generateLinearElementShape` |
| [excalidraw/scene/scroll.ts](https://github.com/excalidraw/excalidraw/blob/v0.18.1/packages/excalidraw/scene/scroll.ts#L14)      | 1336: `isOutsideViewPort`                                                                                                                        |

The [combined replay](https://app.buildbuddy.io/invocation/e9962721-2f83-4dc6-a17e-ca1b09f7dda9)
certified the split at emission and from disk under the structural
normal-completion guarantee, then compared the original and overlaid editor in
Chromium. Its `trial_union` artifacts contain the exact checked `placement.json`,
source deployment origin and both verdicts. A [second replay](https://app.buildbuddy.io/invocation/84bd70d5-5a73-460d-848c-12c6393b6827)
took all 1,787 owner assignments directly from the
[selector export](https://github.com/agentydragon/ducktape/pull/19757).
Forcing its authored import priority as the execution sequence, Lean rejected: “unit 1759 has access to
the moved region of unit 1598”. Using [lean-order.json](lean-order.json) with
`sequential: true`, the same assignments certified at emit and recheck and
passed Chromium. The resolver supplies claims; Lean checks the proposed order.

The [ordinary planner replay](https://app.buildbuddy.io/invocation/04bbaabc-4c15-4759-879c-443526865e13)
used the exported priority to order import requests and let depth-first module evaluation determine
the execution order. With the same source digest and owners, the 236-unit proposal passed
certified emit, disk recheck and Chromium without the order file. The 280-unit utilities
proposal passed the same checks in that run.

## Why this does not move calculateScrollCenter

The [four direct helper selectors](https://app.buildbuddy.io/invocation/5005ee12-5609-421b-b167-436ff5c70ccb)
resolve independently of Rust emission. Adding 1338 (`calculateScrollCenter`)
with the private helpers 1336, 1745 (`getVisibleElements`), 1269
(`getClosestElementBounds`) and 1264 (`getElementBounds`) still leaves the
private class alias in the entry. The [Lean replay](https://app.buildbuddy.io/invocation/906e8fb4-91cd-410f-85ed-0c0d9896379c)
reports one extra public name, `pb$1`, pointing to cell `@1250.0`.

Closing over the bounds dependencies adds the class implementation, its
static initializer and alias (1247–1249), the Bézier helpers and
`getBoundsFromPoints` (1253–1257), and the linear shape and rotated bounds
helpers (1262–1263), together with their shape and geometry dependencies above.
The already selected constants, type predicates and point helpers stay in
their original modules. On that proposal Lean reports
“unit 1338 has access to the moved region of unit 1745”. Selecting 1745 on
its own gives the same refusal. With the complete bounds group but without
1338 and 1745, the checker instead reaches unit 1248: the lowered static
`boundsCache` write allocating a WeakMap is not a local allocating declaration.
Selecting just 1253–1257 leaves unit 1247 able to access 1257; selecting the
first four of those five certifies. Each refusal applies to the tested placement.
