# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

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

A 2026-05-10 profile (consumer
`<spec>/profile_reports/2026-05-10-debundle.md`) showed
`materialize_logical_modules` at 89% of total transform time. The
per-candidate hot-loop wins (binding caches, `BTreeMap → HashMap`,
typed edge IDs, IR cleanup) have landed; reprofile before
picking the next item. Remaining priorities, ordered by leverage:

1. **Compact / stream `owner_graph.json` writes.**
   `write_owner_graph_report` was 6.55 s. The JSON tree gets fully
   allocated before serialization;
   `serde_json::ser::format_escaped_str` was at 5.58% self from
   string-field escaping. Either stream the JSON directly to disk
   (skip the intermediate report tree), or shrink the wire shape.
   The per-owner `purity.reasons[]` arrays added in `a7b3e490`
   add per-non-pure-owner JSON weight that may be opt-in-able.

2. **AST visit churn in `prepare_js_chunks`.** The stage is small
   (~4.5% of total) but `swc_ecma_ast::expr::Expr::visit_children_with`
   shows up repeatedly (~3–4% across entries). Backlog item once
   item 1 lands.

## Graph pass performance and module boundaries

Tighten before the next large peel loop:

- Keep stage telemetry complete (index build/rebuild, fused AST analysis,
  purity, owner-graph construction, quotient construction, validation,
  peelability/report generation, lowering, output writing) — useful
  durations should land in the emitted reports.
- Move repeated timing helpers into one shared Rust module once a second
  pass needs them outside the current local macro sites.
- Add focused regression coverage for `ArtifactIndexes` rebuild boundaries
  as more structural artifact mutations are optimized.
- Profile the debundle action around `materialize_logical_modules`
  and `rename_vendor_exports`; avoid whole-graph clone/rescan patterns
  where a graph pass or indexed lookup can answer the same question.
- Consider changing per-chunk `file_records` from an ordered vector of
  `(file, role)` pairs into a typed map if the output consumers do not
  depend on order. Keep the manifest easy to diff and easy to read.

## Analysis semantics breadth

The focused fixtures exercise the core access model. Validate or extend
behavior for:

- Class fields, static blocks, computed keys, nested function bodies.
- Replayable side-effect attachment.
- Top-level side-effect classification across uncommon initializer shapes.

## Cross-module / imported-binding purity (recursive purity Part 3)

`ChunkCodeGraph` tracks chunk-local function/PlainData purity within
a single source chunk. Imported callees from a different source chunk
(vendor chunks, vite chunk splits) fall through to `unknown_call`
because the importer has no per-function purity for the exporter's
bindings. The downstream cost is each cross-chunk pure-helper needing
a `purity: pure` spec hint in the consumer repo even though its body
would classify pure if it were chunk-local.

Sketch (deferred until residual hint set is dominated by cross-chunk
shapes):

- Per-chunk analysis emits a side-output manifest with each chunk-top
  `Function` and `PlainData` binding's classification.
- When analyzing chunk B that imports `helper` from chunk A's output,
  read A's manifest and seed B's `ChunkCodeGraph` bindings with A's
  per-name verdict.
- Soundness gate: only admit cross-chunk facts when the importer's
  import-specifier names match A's exported chunk-top binding shape
  (e.g. `import { helper }` matches `export const helper = () => …`
  but not a re-export from elsewhere).

Today only one residual cluster in a representative bundle
spec sits cross-chunk (MobX wrapper bindings tracked by the non-emitting
binding-patch stream) and that cluster is the legitimate user of the spec-side
`purity: pure` override — genuinely-impure-but-init-safe vendor
shape, not a pure-by-derivation chain. So Part 3 isn't load-bearing
yet. Land if a future snapshot grows a cross-chunk pure-helper
chain.

Context: <consumer-repo notes on purity recursion>
"Part 3 — cross-module purity" section.

## Corpus breadth

Current passing surface is centered on small synthetic fixtures and the
mock browser bundle. Extend to:

- Large vendor-heavy graphs.
- Unusual dynamic import forms.
- HTML/runtime asset layouts outside the current corpus.

## Rename + lowering pipeline: collect → validate → execute _once_

Design fleshed out at <../../../plans/debundler_rename_lowering_pipeline.md>.
Generalises beyond renames to cover declaration moves, import-specifier
rewrites, export additions, and hoist reordering — same plan-check-execute
seam for every lowering mutation. Tracks the migration path from today's
scattered in-place mutators (PR #1627 / PR #1631 are the canonical
"wrong-era name" symptoms) to a single plan value collected across all
contributors and consumed by a single execute pass.

Cross-references PR1631's `plan_module_reference_needs` reverse-lookup
and PR1627's `normalize_relative_module_specifier` as the two defensive
patches the pipeline retires.

<!--
The "Migrate BindingName = String → swc's hygiene-preserving Id"
entry that used to live here was done by the Id migration PR.
StatementFacts now carries `BTreeSet<Id>` everywhere; `BindingTable`
is gone; `graph.rs` keys binding ownership with `HashMap<Id, OwnerId>`;
reports keep their wire shape via `Atom: Serialize` (atom-only,
SyntaxContext dropped at the JSON boundary).
-->

## Reinvented-wheel audit findings (recorded, no immediate action)

A high-level audit (see chat session
01VmZmgJmMUXFECyQGtsrBMd, after the `ModuleQuotient` Deref-newtype
refactor) surveyed the debundler for places where we hand-roll
something a stdlib / petgraph / swc helper provides. Most findings
came back "appropriate / not actually a reinvention". The ones
below are recorded for visibility, with the explicit decision
that they're not worth doing right now.

### `ChunkTable` interner stays

`ids.rs`'s `ChunkTable` maps chunk paths (`String`) → dense
`ChunkId(usize)` handles. Superficially looks like the same shape
as the retired `BindingTable`, but the value proposition is
different:

- `ChunkId(usize)` is `Copy`, 8 bytes — flows through ~106 sites
  by value. Swapping to `Atom` (the `BindingName → Id` migration's
  natural answer) loses `Copy` semantics: `Atom` is `Clone`-not-
  `Copy` (Arc-backed), forcing `.clone()` at every handle-passing
  site.
- swc's `Atom` global interning is tuned for short repeated JS
  identifiers, not 30-character file paths. The interning win
  evaporates for the actual chunk-key shape.
- The dense indexing is used by storage like
  `Vec<JsChunk>`-indexed-by-`ChunkId.0` and by stable round-trip
  ordering in `ChunkBundle.chunk_order`.

Keep `ChunkTable` as-is.

### `OwnerGraph` hand-rolled CSR stays

`graph.rs`'s `OwnerGraph` stores `Vec<OwnerEdge>` plus
`Vec<Vec<OwnerEdgeId>>` (out_edges / in_edges) CSR adjacency. This
isn't directly replaceable by petgraph because the design
intentionally carries **multiple reasons per `(from, to)` pair as
separate edges**, deterministically sorted by
`(from, to, reason.kind, statement_ordinal, binding)` for stable
report output. petgraph's `DiGraph`/`DiGraphMap` would force a
single edge per pair or push the multi-reason list into a single
edge weight (which is what `ModuleQuotient` does at the quotient
level, where dedup is wanted). Keep the owner-level CSR.

### `LazyBoundary` + `lazy_visit_*` helpers stay

`facts.rs`'s `LazyBoundary` trait and `descend_lazy` /
`lazy_visit_function` / `lazy_visit_class_member` family aren't a
reinvention of `swc_ecma_visit::Visit` — they're a layer **on top
of** `Visit` that lets the collector track lazy vs eager scope
context (function bodies, class instance fields, getter/setter
bodies) without each visitor re-implementing the boundary logic.
The visitor merge (#1671) already collapsed the per-collector
boilerplate to one shared collector that uses the helpers; no
further win available without a generic-walker macro that wouldn't
read cleaner than the current shape.

### Manual `VisitMut` impls in `lowering/` stay

`IdentifierRenamer`, `RenameAndShorthandNaturalizer`,
`ShorthandNaturalizer`, etc. each implement custom `VisitMut`
visitors. These are domain-specific transformations swc doesn't
expose — the right use of swc's `VisitMut` trait, not a wheel
reinvention.

### Dense-int newtypes stay

`OwnerId`, `OwnerEdgeId`, `StatementOrdinal`, `LogicalModuleIndex`,
`ModuleId`, `ChunkId` are all `pub struct Foo(pub usize)` newtypes.
Crates like `typed_index_collections` / `slotmap` would provide
marginal type-system safety on `Vec` indexing, at the cost of a
dep + per-storage-site conversion. Plain newtypes are standard
compiler-IR practice; keep.

### String-based ID round-tripping (`"owner:N"`, `"logical:N"`) stays

`reports.rs`'s `owner_key` / `module_key` / `module_id_from_key`
serialize typed IDs to human-readable strings for JSON reports
and parse them back. Reasonable for the
serialization-boundary use; not gymnastics.

### `HashMap` + post-hoc `sort()` patterns

~40 sites collect into a `HashMap` / `Vec` and `.sort()` for
deterministic output. `BTreeMap` / `IndexMap` would eliminate
the sort at the cost of slightly different iteration semantics.
Most sites are one-shot init / report generation (not hot path);
worth converting a few specific report-generation sites to
`BTreeMap` for semantic clarity (the sorted order is the point,
not an afterthought), but no urgent action.
