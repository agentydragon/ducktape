# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

The big architectural item — replacing the legacy init-wrapper
lowering with a static analysis engine + source-order emit —
landed in phases 1 → 4 (see <DESIGN.md> "Status" table),
including the `BindingKind::Imported` collapse that closes out
the parallel `import_members` channel. The items in this file
are smaller, mostly-orthogonal cleanups.

## Excalidraw live-browser smoke

Build an open-source live-browser smoke test for the debundler against
a Bazel-managed Excalidraw bundle. The motivation: when a debundler
issue surfaces against a private upstream corpus (proxy crash, AST
corruption, missing chunk, emit shape regression, optimisation
behaviour bug), reproducing the failure on Excalidraw lets us share
the repro in a public bug report, write a regression test that runs
in ducktape's open CI, and avoid leaking proprietary upstream bundle
detail. Excalidraw is open-source and broadly representative of "real
React + vendored chunks + dynamic imports + service worker."

### Bundle build

Use a Bazel-managed Excalidraw build. Two viable paths:

- **Pull a prebuilt deploy** (snapshot a specific `excalidraw.com`
  publish). Requires keeping the snapshot fresh enough that
  upstream's auxiliary endpoints (if any) still work. Easier to
  bootstrap.
- **Build from source under Bazel** — Excalidraw's `excalidraw-app/`
  builds with Vite; reproduce that build (npm + vite via
  aspect_rules_js, or via a `genrule` shelling to npm) and feed the
  output into the pipeline. More work upfront, but the bundle is
  reproducible from a single git pin and we control the optimisation
  level / minifier settings.

Either way, the build configuration must produce a realistic
production bundle: minify on, identifier renames,
production-tree-shaken, real chunk-split boundaries. A development
build (with un-mangled names and source maps inlined) won't exercise
the debundler's RE-relevant code paths.

### Spec scope

Not the round-trip minimum — the spec should exercise realistic-ish
module extraction and rename paths, the same shape a private-corpus
spec runs. Concretely:

- `apply_vendor_annotations` + `swap_vendor_chunks` over a couple of
  Excalidraw's actual vendor chunks (React, Roughjs, Pointers, etc.)
  so vendor-swap edge cases (`named_from_default`,
  `named_from_module_default`, default-only, JSON-default) get covered
  on a real bundle, not only synthetic fixtures.
- `materialize_logical_modules` over a handful of pre-identified
  Excalidraw source modules — pick ones whose shape is recognisable
  in the compiled output (a clearly-bounded React component, a pure
  geometry helper, a state slice). Goal: prove the materialiser
  recovers approximately the right symbols/files from a real
  scrambled bundle.
- A small set of `logical_modules` rename entries on identifiers
  visible in the compiled output. Goal: exercise the rename pipeline
  at realistic aggressiveness.
- `emit_browser_harness`, relying on the always-on
  `rewrite_chunk_entry_specifiers` transform, so the output is a
  runnable app the live proxy can serve.

The exact module list / rename list is part of the implementation —
pick stable shapes that are unlikely to drift wildly when Excalidraw
upgrades. Stale picks become a self-test: if the materialiser fails
to find them, that's a real signal (either the bundle moved or our
matchers regressed).

### Smoke target contract

- runs `bazel test //devinfra/js/debundle/excalidraw:load_test` (or
  similar);
- builds the Excalidraw bundle through the shared `debundle_pipeline`
  rule (<pipeline.bzl>);
- starts the live-proxy binary against the resulting harness;
- drives a headless Chromium through the proxy, asserts:
  - no failed asset requests,
  - no console errors,
  - the canvas toolbar is visible (e.g. `[data-testid="toolbar"]`
    or whichever stable selector Excalidraw exposes),
  - a small interaction works (click the rectangle tool, click on
    the canvas, verify a shape was added — proves the React app is
    reactive after debundle).

### Hosting

Self-hosted, no MITM. Private-corpus smokes generally have to MITM the
live host because their auth/data is server-side; Excalidraw runs
entirely in the browser, so a self-hosted bundle is a fully working
app. Self-hosting removes the network dependency and CDN-rotation
flakiness (the test stays green even when excalidraw.com is down) and
matches the "reproduce against Excalidraw" workflow goal — a public,
deterministic smoke that doesn't depend on third-party uptime.

### Workflow rule

When a private-corpus debundler issue is tractable on Excalidraw too,
prefer landing the regression test on this Excalidraw target (or a
smaller minimised e2e under `devinfra/js/debundle/e2e/`) rather than
only fixing it behind the private repo. The latter loses the
public-CI signal and the public-bug-report leverage.

## RE coverage side-output

Implemented in <identifier_rename_queue.rs> as a side output of every
manifest-emitting pipeline run (`emit_browser_harness`, `write_js_tree`).
The JSON lands at `<out_dir>/identifier-rename-queue.json` and
the path is recorded on the stage's manifest under
`identifier_rename_queue`.

Queue membership is origin-based, not spelling-based: a final output
top-level binding is queued when it still has a name recorded from the
input bundle for that chunk. Once the spec gives the binding a readable
output name, it leaves the queue.

## Logical materialization breadth

The current `materialize_logical_modules` covers top-level
function/class/variable declaration movement and explicit owner assignment.
Still to do:

- Full lowering matrix: binding placement reports, attached side-effects,
  staged-shell edge cases beyond the focused fixture.
- Owner-fragment modeling parity for nested declarations and re-exports.
- Keep new analysis tooling on the existing owner-graph and peelability side
  outputs; do not add parallel selected-owner cache formats.

## Materialize-stage hot-loop optimizations

From `perf record` against the Tana debundle pipeline (Gaffer
`tana/re/web/spec/profile_reports/2026-05-10-tana-debundle.md`):
`materialize_logical_modules` was 89% of the total transform
time, dominated by `build_owner_graph_report` (22.54 s) and
`write_owner_graph_report` (6.55 s).

Landed:

- `Schedule::entry_exported_binding_names` cache passed by
  reference into `peel_emit_blocked_residual_bindings`.
- Precomputed `Schedule::export_name_by_binding` map driving
  `binding_reports`.
- Per-candidate peelability maps switched from `BTreeMap`/`BTreeSet`
  to `HashMap`/`HashSet`.
- `OwnerEdgeEntry::id` `String` field dropped in favor of a typed
  `OwnerEdgeId(usize)` derived from the entry's slice index.
- `Schedule::bindings`, `Schedule::chunk_renames`,
  `LogicalModule::rename_map`, `BindingKind::Imported::re_exported_by`,
  and `ModulePlan::bindings` switched to `HashMap`. Emit / report
  sites that depend on iteration order sort at the boundary, and
  `chunk_renames` validation now collects every violation before
  bailing (single error → full punch list).

Reprofile against Tana before picking the next item.

Remaining priorities, ordered by leverage:

1. **Compact / stream `owner_graph.json` writes.**
   `write_owner_graph_report` is 6.55 s. The JSON tree gets fully
   allocated before serialization;
   `serde_json::ser::format_escaped_str` is at 5.58% self from
   string-field escaping. Either stream the JSON directly to disk
   (skip the intermediate report tree), or shrink the wire shape.
   The per-owner `purity.reasons[]` arrays added in `a7b3e490`
   add per-non-pure-owner JSON weight that may be opt-in-able.

2. **AST visit churn in `prepare_js_chunks`.** The stage is now
   small (679 ms / ~4.5% of total) but
   `swc_ecma_ast::expr::Expr::visit_children_with` shows up
   repeatedly at 0.55%–1.16% self entries (totalling ~3-4%).
   Backlog item once item 1 lands.

## Graph pass performance and module boundaries

The current owner-graph framing is in place and lives in flat, concept-named
analysis files. Some passes should still be tightened before the next large
Tana peel loop:

- Keep telemetry complete as the pipeline keeps moving toward functional
  per-chunk products: index build/rebuild, fused AST analysis, purity, owner
  graph construction, quotient graph construction, validation,
  peelability/report generation, lowering, and output writing should all have
  useful durations in the emitted reports.
- Move repeated timing helpers into one shared Rust module once a second pass
  needs them outside the current local macro sites.
- Continue moving hot-path code toward typed ids and array lookups where the
  data is chunk-local; keep strings at spec, diagnostic, and serialization
  boundaries.
- Add focused regression coverage for `ArtifactIndexes` rebuild boundaries as
  more structural artifact mutations are optimized.
- Profile the Tana debundle action around `materialize_logical_modules` and
  `rename_vendor_exports`; avoid whole-graph clone/rescan patterns where a
  graph pass or indexed lookup can answer the same question.
- Consider changing per-chunk `file_records` from an ordered vector of
  `(file, role)` pairs into a typed map if the output consumers do not depend
  on order. Keep the manifest easy to diff and easy to read.

## Parser / analysis ownership audit followups

Syntax-derived facts should come from the SWC-backed parse/analysis path and
then flow through artifact manifests or indexes. Do not add phase-local string
scans, regexes, or miniature JavaScript parsers to answer questions the real
parser already answered.

Known cleanup targets:

- Keep import/specifier facts downstream of `prepare_js_chunks`: the pipeline
  should parse/analyze chunks once, then derive vendor-caller and chunk-reference
  decisions from `ChunkManifest.imports` / artifact indexes. No pre-parse source
  scanning for import strings.
- `vendor.rs` still has AST-local import alignment and vendor import-renaming
  walks. The mutation pass needs an AST visitor, but the "which source imports
  which chunk / which named bindings" facts should be fed from artifact import
  indexes rather than rebuilt by ad hoc per-stage AST scans.
- `program_analysis.rs` currently records dynamic-import count but not dynamic
  import source records. If later pipeline decisions need dynamic import
  sources, extend the shallow analysis schema once instead of discovering them
  again in another stage.
- `rewrite_specifiers.rs` and vendor import rewriting duplicate source
  resolution logic around relative/source import references. Centralize the
  resolution/index query API so mutation stages ask the same typed question and
  differ only in how they rewrite the matched AST node.

## Vendor swap edge cases

- `named_from_default`: documented acceptance contract is "upstream
  default export is an object literal whose keys are `Prop::KeyValue`
  with `Ident` or `Str` names". `e2e/vendor_swap_test.rs` pins the
  accepted shape and explicitly rejects two adjacent shapes:
  - **Shorthand props** (`export default { ping, pong }` where `ping`
    and `pong` are local refs) are silently skipped by
    `collect_default_export_object_keys` and the wrapper fails the
    missing-keys check. Real-world vendor `index.mjs` files commonly
    use shorthand, so accepting them is a useful relaxation. Fix:
    extend the prop-walk to read shorthand keys from the prop's own
    name. Should be a few-line change with one new accepted-shape
    test.
  - **Class / function default declarations** (`export default class
{}`) don't surface as `ExportDefaultExpr` and bail with "no
    export default declaration". Open whether to accept (less common
    for `named_from_default` wrapper shape) or document as
    intentionally out-of-scope.

- `named_from_module_default`: handle anonymous default function/class
  declarations (currently rejected).

## Analysis semantics breadth

The focused fixtures exercise the core access model. Validate or extend
behavior for:

- Class fields, static blocks, computed keys, nested function bodies.
- Replayable side-effect attachment.
- Top-level side-effect classification across uncommon initializer shapes.

## Corpus breadth

Current passing surface is centered on small synthetic fixtures and the
mock browser bundle. Extend to:

- Large vendor-heavy graphs.
- Unusual dynamic import forms.
- HTML/runtime asset layouts outside the current corpus.

## Authoring config: browser-harness asset root

Add a separate browser-harness asset root only if a real corpus needs static
asset inputs to come from somewhere other than `inputs.root`.
