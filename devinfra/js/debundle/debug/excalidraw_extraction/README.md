# Excalidraw 0.18.1 extraction boundaries

Probes from November 12–13 that tried to move canonical Excalidraw bundle code
back into its upstream module homes. Each claim below resolves through
structural `source_match` selectors; the **rejection is in module assignment,
not matching**. Unit IDs are scoped to the pinned chunk named.

## Shared constraint: the 410-unit editor group

Most probes hit one 410-unit `editor` atomic group spanning the residual entry.
Its witnesses share a shape: from residual `unit:675` through `unit:1206`, then
back through sequenced `binding_timeline` fallback edges whose source summary
cites `callback_pool_limit` (limit 64; pool sizes 316–479) or a
`possible_getter` promotion. These are current-analysis constraints, **not
proof of an actual upstream initialization cycle**.

Common next step for every row: trace the getter/callback-publication path,
reduce the rejection to a neutral CLI fixture, then choose a tool repair or an
audited initialization contract. Reclaim a binding only when the full gate and
emitted browser/Node behavior pass together. Keep each binding's original
module home; copying the whole residual group there would obscure ownership.

To reproduce a row: add its claim to the named spec file under
`excalidraw/spec/modules/`, then build `//devinfra/js/debundle/excalidraw:debundle`.

| Upstream home (spec file)                                    | Still residual (unit)                                                                                                                           | Claim to add                                                                                                                                                                                   | Witness                                                                                                                                                                                                    |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data/blob.ts` (`editor/packages/excalidraw/data/blob.yaml`) | `getFileHandleType` (`1752`, `Bm`), `isImageFileHandle` (`1754`), `isSupportedImageFileType`, `createFile`; the other 21 bindings pass          | structural selectors for the four declarations                                                                                                                                                 | `675 → 1206 → 1689` (`validateIndicesThrottled`) `→ 1752`; return via `writer_after_reader` to `1517`, `possible_getter` with lazy owner `1754`, then `1132 → 675` citing `callback_pool_limit` (pool 316) |
| `element/bounds.ts` (`…/element/bounds.yaml`)                | `getBoundsFromPoints` (`1257`), `getArrowheadSize` (`1259`), `getArrowheadAngle` (`1260`), `getCenterForBounds` (`1272`)                        | selectors for the four functions; `getVisibleSceneBounds`/`doBoundsIntersect` already pass                                                                                                     | `675 → 1206 → 1257`, sequenced edges back                                                                                                                                                                  |
| `scene/Scene.ts` (`…/scene/Scene.yaml`)                      | `getNonDeletedElements` (`1688`), `validateIndicesThrottled`, `hashSelectionOpts`, `isIdKey`, static-map writes (`1693`); class (`1692`) passes | the helpers and the `sceneMapByElement`/`sceneMapById` writes. Class + writes rejects with only `1693` in the group; the three helpers reject alone; dropping the throttle alone still rejects | `675 → 1206 → 1688`, sequenced fallback edges back (pools 398, 316)                                                                                                                                        |
| `fonts/ExcalidrawFontFace.ts`                                | `ASSETS_FALLBACK_URL` init (`881` class body passes; `882`)                                                                                     | `anonymous_statements[].source_match.match`: `ANYTHING(ExcalidrawFontFace, "ASSETS_FALLBACK_URL", ANYTHING);`                                                                                  | 410-unit group                                                                                                                                                                                             |
| `element/linearElementEditor.ts`                             | `unit:1387`: `POINT_HANDLE_SIZE` + six static function properties (class body `1386` passes)                                                    | one `anonymous_statements` match for the seven calls in order, `LinearElementEditor` as receiver, `ANYTHING` for helper and values                                                             | `1387 → 5` (`po`) via sequenced binding timeline; fallback after `callback_pool_limit` (pool 479); property-definition helper calls keep `unknown_call`                                                    |
| `element/mutateElement`                                      | `1696`                                                                                                                                          | source-validated candidates                                                                                                                                                                    | 410-unit group                                                                                                                                                                                             |
| `scene/selection`                                            | `1685` (`getSelectedElements`)                                                                                                                  | 〃                                                                                                                                                                                             | 〃                                                                                                                                                                                                         |
| `scene/comparisons`                                          | `1222` (`canChangeRoundness`)                                                                                                                   | 〃                                                                                                                                                                                             | 〃                                                                                                                                                                                                         |
| `element/heading`, `element/distance`, `element/sizeHelpers` | `1225`–`1236`; `1274`; `1699`, `1704`                                                                                                           | combined geometry probe across all three                                                                                                                                                       | 〃                                                                                                                                                                                                         |
| `binaryheap`                                                 | `1551` (`BinaryHeap`)                                                                                                                           | 〃                                                                                                                                                                                             | two edges: `1551 → 1192` sequencing, back via eager use                                                                                                                                                    |

Related groups outside the 410-unit one:

- **`packages/math/utils.ts` `average`** (`editor/packages/math/utils.yaml`):
  add `average` to `bindings`; its template already matches. Rejects with an
  82-unit group; the other 31 math bindings pass. Cut: `unit:485` (`rr`, the
  arrow initializer) `→ unit:19` (`Ml`), sequenced `binding_timeline`
  `writer_after_reader`, reader attributed to `opaque_callback_invocation`. The
  residual endpoint calls `ra` during bundled vendor initialization. Determine
  whether current host assumptions allow that reentry or the callback pool
  attributes a read of an unpublished binding without a realizable path.
- **`main` library atom**: inside a 262-unit `main` initialization group.
- **`getGridPoint`, `getSizeFromPoints`, `rescalePoints`**: inside the 410-unit group.

Incomplete probes: initial collision and crop templates failed source
matching, so they establish no ownership limit; repair the templates against
native AST output before gating. The two extracted `transformHandles` device
helpers import the browser entry; their runtime probe belongs in `test_smoke`,
not an import-free Node probe.

## Coverage checkpoint (dated 2026-11-13)

**Stale:** later recoveries (e.g. App component, 73 constants #19739, 55 editor
utilities #19760) are not counted. Re-run the procedure for current numbers.

Metric: named-module emitted bytes / all emitted `.js` bytes in the `main` and
`editor` chunks (residual entries in the denominator; bundled dependencies
included; vendor, locale and font/WASM chunks excluded). Not coverage of the
whole shipped corpus or upstream TypeScript.

| Measure                                    | 2026-11-12                  | 2026-11-13                   |
| ------------------------------------------ | --------------------------- | ---------------------------- |
| Named / all main+editor emitted bytes      | 124,063 / 2,209,440 (5.62%) | 225,596 / 2,224,659 (10.14%) |
| Named source units / all main+editor units | 164 / 3,415                 | 236 / 3,415                  |
| Original module homes                      | 29                          | 38                           |

Procedure, on the `//devinfra/js/debundle/excalidraw:debundle` output tree:

1. For `main` and `editor`, read `reports/tree/<chunk>/modules.json`.
2. Sum each non-residual `final_module_contents[].file` under `app/<chunk>`.
3. Divide by the bytes of all emitted `.js` files under those two directories.
4. Count distinct `(chunk, source_unit_id)` pairs for the unit metric; the
   denominator comes from each chunk's `graph_summary.json`.
