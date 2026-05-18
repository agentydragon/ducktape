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

## Rename pipeline: collect → validate → execute _once_

The naturalizer / lowerer currently mutates identifiers in place across
several independently-discovered passes and lets every downstream consumer
(import planning, cross-module binding lookup, fact collection,
source-map fragment emission, export tables) read whatever name happens
to exist when _it_ runs. PR #1627 (`object_literal_import_collapse_test`,
fixed by e0b9c7f) and PR #1631
(`object_literal_return_shorthand_drops_import_test`, fixed by the
heuristic-rename reverse-lookup at
`logical_modules.rs::plan_module_reference_needs`) are both symptoms of
the same shape: a rename happened in one layer and a downstream layer
keyed off the wrong-era name. Each fix has been a localized defensive
patch on the consumer side, which leaves the same trap waiting for the
next consumer that doesn't yet know about the rename.

### Current state (post PR #1651)

Hygiene-aware identity is fully wired through the chunk-level
pipeline. SWC's `resolver` pass runs on every parsed module
(`js_ast::parse_and_resolve`); `swc_common::GLOBALS` is set at
`main.rs` / `run_agent` and propagated into every rayon `par_iter`
worker. Every chunk-scoped identifier map keyed by binding identity
is `Id`-keyed:

- `RuntimeImportFacts.imports: HashMap<Id, RuntimeImportInfo>`.
- `ModuleBodyFacts.{imported_locals, provided_locals, referenced_idents}: HashSet<Id>`.
- `RefCollector.ids: HashSet<Id>`.
- `Schedule.{bindings, chunk_renames, export_name_by_binding}` (`schedule.rs:31,38,70`).
- `ScheduleLogicalModule.rename_map: HashMap<Id, Atom>` (`ids.rs:195`).
- `ChunkAstAnalysis.{declaration_by_name, pre_existing_entry_exports}`.
- `binding_assignment: HashMap<Id, usize>` threaded through `LowerChunkInputs` with `chunk_top_level_mark`.
- `entry_exports_by_original_local: HashMap<Id, EntryExport>`.
- `eq_ignore_span`-based matchers strip `SyntaxContext` via
  `SyntaxContextStripper` for cross-parse AST comparison
  (`resolve_anonymous_statement_ordinals`).

What's left of the PR #1633 reverse-lookup bridge in
`plan_module_reference_needs`: it still reconstructs the provider's
pre-rename `Id` by pairing the heuristic-rename pre-sym with the
body `Id`'s own ctxt. That bridge will be retired by PR2's
`RenameLedger` (below) — the ledger holds the full pre→post mapping
so no caller needs to reconstruct anything.

The heuristic `local_renames: BTreeMap<String, String>` from
`naturalize_module_body` is also still String-keyed; it's the
contributor side of the rename pipeline that PR2 redesigns.

### Proposed architecture (PR2)

The proposed architectural fix is a single **collect → validate → execute
(once)** rename pipeline:

- **Collect**: every rename contributor submits _intents_ into a single
  buffer instead of mutating the AST. Contributors include explicit
  spec-specified renames, naturalizer heuristics (return-object alias
  inference, shorthand-collapse readback, future readable-name
  autonaming), import-induced renames (`{ sA as propKeyA }`),
  collision-resolution renames, and chunk-level renames produced by
  factorize. Each intent is `(scope, original_name, new_name, reason,
priority, invariants_it_assumes)`.

- **Validate**: resolve conflicts deterministically before any AST
  mutation. Priority order is explicit > import-induced > heuristic.
  Surface contradictions (`sA → propKeyA` _and_ `sA → propKeyB` in the
  same scope) as hard errors at validation time, not as silent
  last-write-wins behavior at AST mutation time. Output is a stable,
  read-only mapping `(scope, original_name) → final_name` and the
  inverse `(scope, final_name) → original_name`.

- **Execute once**: one pass applies the resolved mapping to the AST
  _and_ updates every fact table (`runtime_imports`, `referenced_idents`,
  export tables, source-map fragments, cross-module binding indexes) in
  lockstep, keyed by the original name. After execute, no later pass
  invents a rename — every consumer that needs to bridge between
  pre-rename and post-rename names consults the same finalized mapping.

Direct consequence: the family of "X-layer renamed it, Y-layer didn't
notice" bugs collapses to one architectural seam. The
`plan_module_reference_needs` reverse-lookup that fixes #1631 becomes
dead code (the final mapping makes both the body AST and the
`runtime_imports` map agree on a single set of names before planning
runs), and similarly `normalize_relative_module_specifier` from the
#1627 fix could rejoin a normal path-building step rather than living as
a defensive sanitizer at the usage site.

Prerequisite work before designing the pipeline:

1. Inventory every current rename contributor in `logical_modules.rs`
   and adjacent files. Examples already known:
   `collect_return_object_alias_renames` (line ~3044),
   `collect_naturalization_renames_from_function` (~2982), the
   `RenameAndShorthandNaturalizer` `VisitMut` (~3172),
   `naturalize_object_literal_shorthand` (~3226),
   `disambiguate_import_locals` (~3668), the chunk-level
   `chunk_renames` map that flows out of factorize, and any
   collision-resolution code path that mutates `module_import_renames`
   at the orchestration site. Capture each contributor's _kind_
   (explicit / heuristic / collision / chunk-level), _scope_ (function
   / module / chunk / cross-chunk), and _current side-effect surface_.

2. Inventory every downstream consumer that today reads identifier
   names off the AST or off pre-rename fact maps. Same call sites that
   currently need defensive bridging.

3. Decide on the scope model. Per-function naturalizer renames don't
   need to be visible at chunk scope; chunk_renames don't need to
   reach per-function naturalizer collection. The intent buffer should
   reject cross-scope writes by construction.

4. Design must not block on landing #1631-style defensive fixes — those
   should land case by case as they're discovered, with this TODO
   updated to note when a defensive patch becomes architecturally
   redundant. Removing the defensive patches is part of the
   pipeline-landing cleanup, not the architecture work itself.

Likely multiple PRs. Should land _after_ the `Schedule` →
`ChunkFactorization` rename below, because the partition-and-factorize
surface is where the chunk-level rename intents already live and
restructuring on top of the renamed type is cheaper than restructuring
twice.

## Rename `Schedule` → `ChunkFactorization`

`schedule.rs::Schedule` is, after the factorize rewrite, just a
per-chunk factorization: the authoritative `Partition` plus its
derived realizability views (`dep_graph`, `linker_order`,
`assembly_conflicts`) plus a pile of caches that hot-path callers
read repeatedly (peelability evaluates ~1500 candidates/chunk; the
`linker_position_by_module`, `export_name_by_binding`, and
`entry_exported_binding_names_cache` exist to keep that loop
cheap). The "Schedule" name dates to when the central object was
the ECMA-262 link-time evaluation order — now the partition is the
SSOT and everything else is derivable.

Rename to `ChunkFactorization` (or similar) and update docstrings.
Mechanical sweep: ~80 references across the crate. Should land in
its own commit so the rename is reviewable in isolation; no
behavioral change. Wait until any in-flight Schedule-touching work
(the precompute-atomic-units constructor) is settled before
starting.

If a deeper restructure feels worthwhile when picking this up,
consider splitting the type into `FactorizationInputs` (facts,
bindings, logical_modules, chunk_renames) + `Factorization`
(partition + dep_graph + linker_order + conflicts) +
`FactorizationCaches`. Probably not worth the boilerplate, but
evaluate before doing the rename.

## Id-migration code-reduction follow-ups

PR #1639 migrated chunk-scoped identifier maps to SWC `Id`-keyed. PR
#1647 closed the easy ergonomic wins. What remains is bounded by
design choices documented in DESIGN.md.

- **`ident.sym.to_string()` sites (~35).** Three sub-categories with
  different verdicts:
  - **Spec/report surfaces** (`facts.rs` visitor collectors,
    `purity.rs`, `strip_swapped_vendor_exports.rs`,
    `vendor.rs:707-711`). Values terminate in `StatementFacts.*` /
    vendor manifests / report JSON. `BindingName = String` is pinned
    by DESIGN.md "Identifiers and types" (line 2594) because it IS
    the JS identifier text. **No action.**
  - **Name-disambiguation surfaces** (`lowering/util.rs`'s
    `collect_occupied_local_names` / `collect_local_binding_names`,
    consumed by `disambiguate_import_locals`). The result feeds
    collision-avoidance logic that emits JS identifiers. These
    `BTreeSet<String>` could become `BTreeSet<Atom>` (`Atom` impls
    `Hash`/`Ord`/`Display`) — the conversion at the emit boundary
    is `format!("{atom}")`. Stylistic cleanup only; semantically
    identical. **Optional.**
  - **Spec → Id boundary** (the `declaration_by_name` /
    `binding_assignment` / `pre_existing_entry_exports` /
    `entry_exports_by_original_local` maps). **Done in PR #1651.**
- **Per-test `js_ast::with_swc_globals(|| {...})` wraps (~14).**
  In-process tests across `pipeline.rs`, `validate_emitted_exports.rs`,
  `vendor.rs` each wrap their body in `with_swc_globals`. The per-test
  wrap is a deliberate design choice (explicit setup over defensive
  auto-wrap) and preserves mark-identity across calls. A consolidation
  via proc-macro attribute would obscure the wrap without changing
  the underlying contract. **No action unless a future API change
  makes the wrap implicit safely.**

## RenameLedger (PR2) open questions

Before implementing the "Rename pipeline: collect → validate →
execute _once_" architecture above, pin these three design questions:

1. **Conflict policy for same-priority heuristic disagreements.** When
   two heuristic contributors propose conflicting renames at the same
   priority (e.g. `collect_return_object_alias_renames` says `sA →
propKeyA` and `collect_naturalization_renames_from_function` says
   `sA → propKeyB`), does the ledger panic at seal citing both
   submitters, or silently suppress the lower one? Default proposal:
   panic loudly, with both contributors named in the error — silent
   suppression is the trap PR2 is meant to close.
2. **Disambiguation name minter.** `disambiguate_import_locals` today
   appends `_1`, `_2`, ... suffixes until a free name is found. When
   that becomes a `RenameLedger` method (so the ledger owns "what
   names are taken in this chunk"), does the scheme stay as-is, or
   switch to something more readable (`name_from_module`, etc.)?
   Default proposal: keep `_N` scheme; readability is a separate
   concern best handled by a later naturalizer pass.
3. **Structural mutations during COLLECT.** Today
   `materialize_logical_modules` moves declarations between modules
   in-place during the collect phase. Tighten the contract to either:
   - "no structural moves between seal and execute" (pragmatic — most
     moves already happen pre-seal, and the type-level barrier
     `&Module` in non-execute passes lands cleanly), or
   - "all structural moves pre-COLLECT" (cleaner architecturally but
     requires reordering the materializer to compute a final body
     order before any rename intents are submitted).

## lowering/ module ergonomics

PR #1647 cleared the `#[allow(unused_imports)]` and the
`#[macro_export]` on `time_phase!`. One item remains:

- **PR #1633 reverse-lookup bridge** (`plan_module_reference_needs`
  reconstructs pre-rename `Id` by pairing the heuristic-rename
  pre-sym with the body Id's own ctxt). Retired by PR2's
  `RenameLedger` when the ledger makes pre-rename and post-rename
  names available without bridging. Track the removal as part of PR2
  landing, not as an independent cleanup.
