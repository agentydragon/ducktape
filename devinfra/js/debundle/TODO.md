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

- `vendor` marks with `level: swap` over a couple of
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
- `emit_browser_harness`, relying on the always-on emission-time
  specifier canonicalization, so the output is a runnable app the
  live proxy can serve.

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

## Anonymous selector indexing in graph dumps

Today `anonymous_statements:` selectors resolve purely by AST-shape match
against the chunk's top-level statements (`match` / `source_match`); the
spec format has no owner field for them, so nothing leans on
author-provided owner hints. That keeps the format honest, but every edit
gate / coverage check touching anonymous statements has to re-parse source.

If that source resolution ever becomes too expensive, extend
`owner_graph.json` with enough machine data to resolve anonymous selectors
from the dump itself — a canonical emitted-JS string or AST fingerprint per
anonymous owner, keyed by owner id and statement ordinal — so CLI tools can
match `anonymous_statements[].match` against graph-owned statements without
reading source files. This is conditional on hitting that cost; not yet
observed. (The `owner:<id>`-in-spec-notes framing that used to live here was
stale — no such mechanism exists; `owner:N` ids are machine-generated graph
keys and a `describe`/`show-source` input, not a spec authoring hint.)

## Completed-plan follow-ups

- **Peel proposer contraction cleanup.** The quotient-contraction
  proposer and lazy-priority-queue greedy driver are implemented and
  documented in <docs/peel_proposer.md>. Remaining cleanup is narrow:
  retire the hidden full-scan greedy reference only after the lazy-PQ
  path has had release-cycle confidence, and add a diagnostic-only seed
  rejection mode only if spec authors need a focused debugging surface.
- **Chunk IR / schedule split remains deferred.** The completed IR cleanup plan
  left one deliberately deferred refactor: split the "everything about chunk K
  under partition P" state into a pure `ChunkIR` plus a `Schedule` only if a
  downstream consumer actually needs one half without the other. If this happens,
  move the `owner_report_ids_by_binding` cache to its report-only consumer and
  have peelability build directly from `(ir, partition)`.
- **Inter-cell cycle policing at assignment/render time.** The exact gate does
  not reject mutually-cyclic residual cells itself; `peel/factorize.rs` reports
  them as `BlockedResidualDependency`. If that is insufficient for
  `bindings assign`, add a render-time cell-DAG / proposal-shape check rather
  than reintroducing a realizability-gate rule.
- **Proptest coverage for the gate differential harness.** Migrate
  `peel/gate_differential_test.rs` from its deterministic xorshift sweep to
  proptest strategies, matching the condensation-order and `RenameLedger`
  proptest suites.
- **Materialize-into-emit.** Let lowered outputs feed `write_js_tree` /
  harness emission directly, dropping the `materialize_logical_modules`
  bundle round-trip and post-materialize index rebuild. Details live in
  <ARCHITECTURE_BACKLOG.md>.
- **Post-strip consumer scan retirement.** Retire
  `vendor/mod.rs::validate_partial_swap_consumers` only after construction
  paths consult `VendorResolutionPlan` and e2e fixtures pin synthesized
  consumer-directive shapes that fail without the scan. Details live in
  <ARCHITECTURE_BACKLOG.md>.
- **Program facts unification.** Fold
  `program_analysis.rs::analyze_program_shallow` into the full program-facts
  path, or derive it from that path, so the two top-level fact walks cannot
  drift. Also tracked in <CODE_REVIEW.md>.
- **Sanitization-era test cleanup.** Clean up the remaining small fixture debt:
  consolidate the `logical_module_with_*` helpers in `e2e/support.rs`, remove
  the hardcoded entry path from
  `assert_generated_module_after_entry_script`, and replace brittle
  whitespace OR-chain assertions. Also tracked in <CODE_REVIEW.md>.

## Structural selector language

`source_match` and same-module `binding_groups` exist, but the selector
language still needs more shape when porting real minified bundles. Keep
new features generic and backed by synthetic e2e fixtures; do not encode
private corpus details in Ducktape.

- **Contextual selectors.** Add readable `before` / `after` / `near`
  anchors for cases where the selected statement or declaration is
  helper-boilerplate that appears multiple times. This should replace
  copied overlapping selector bodies and still reject ambiguous windows.
  Decorator-helper neighborhoods are the motivating generic shape: a
  helper declaration may be syntactically identical across many classes,
  but it becomes unambiguous when selected near the class or decorator
  calls whose property strings identify the target.
- **Constrained hole matching.** Multi-hole selectors need cheap local
  constraints so authors can say that a hole appears only as a specific
  argument, callback body, object property value, or statement-list slot. This
  should avoid scanning unrelated subtrees while preserving the current rule
  that ambiguous matches are hard errors.
- **Cross-module binding groups.** Current `binding_groups` export
  multiple bindings into one logical module. Add or design a form for
  one matched declaration context whose bindings should land in
  different modules, without repeating the whole source selector.
- **Selector debt reporting.** Shipped as `debundle spec selector-debt`:
  ranks name-only selectors by how minified the bound name looks, groups
  `source_match` bodies copied verbatim across members / anonymous
  statements / binding groups, and (via `--against`) diffs minified
  bindings across two spec versions. Still open is the **source-aware**
  column — flagging _near-ambiguous_ structural matches (a `source_match`
  that matches exactly one statement today but is one drifted subtree away
  from matching two). That needs the chunk AST / owner graph, not just the
  modules tree, so it is a separate report from the current spec-only one.
- **Selector linting.** The report now exists (`spec selector-debt`); the
  next step is an opt-in warning or gate that fails when a name-only
  selector over an autogenerated/minified source scores above a threshold,
  with an explicit escape hatch for cases where the name is genuinely
  stable or no better structural handle exists.
- **Matcher core cleanup.** Factor anonymous-statement and member-binding
  `source_match` resolution through a shared parse/canonicalize/window
  matcher before adding more selector variants.

## Logical materialization breadth

The current `materialize_logical_modules` covers top-level
function/class/variable declaration movement and explicit owner assignment.
Still to do:

- Full lowering matrix: binding placement reports, attached side-effects,
  staged-shell edge cases beyond the focused fixture.
- Owner-fragment modeling parity for nested declarations and re-exports.
- Keep new analysis tooling on the existing owner graph and embedded atomic
  DAG side outputs; do not add parallel selected-owner cache formats.

## Performance

Proposer-gate, `debundle run`, and materialize-stage performance work
all live in <perf/proposer.md>. Known lowering-side items not yet in
that log:

- `JsChunk::{get_file,get_file_mut,remove_file}` (`artifact.rs`) are
  linear scans over `files`, so passes that touch every file go O(n²)
  per chunk. A path-keyed index fixes it.
- `split_entry_body` (`lowering/lower.rs`) clones every retained
  statement out of the chunk AST even though the chunk body is
  replaced wholesale afterward; a draining/move-based split avoids
  the full-AST clone.

## Factorize / atomic-DAG docs drift

- **"Factorize" remains overloaded.** Broadly, factorization/assembly
  produces the authoritative owner partition (`factor_assembly.rs`),
  while `peel/factorize.rs` produces advisory planner proposals from
  the serialized atomic DAG (surfaced as `debundle modules propose`).
  Keep docs explicit about which one they mean.

## Analysis semantics breadth

The focused fixtures exercise the core access model. Validate or extend
behavior for:

- Class fields, static blocks, computed keys, nested function bodies.
- Replayable side-effect attachment.
- Top-level side-effect classification across uncommon initializer shapes.

The purity-classifier backlog (cross-chunk purity facts, block-bodied
enum IIFE forms, statement-level overrides, compositional proof) lives
in <x/purity_recursive.md>.

## Corpus breadth

Current passing surface is centered on small synthetic fixtures and the
mock browser bundle. Extend to:

- Large vendor-heavy graphs.
- Unusual dynamic import forms.
- HTML/runtime asset layouts outside the current corpus.

## Rename pipeline

The collect → seal → execute-once `RenameLedger` pipeline landed 2026-06
(PRs #2086/#2091/#2101/#2106 plus the PR-5 defensive-era cleanup);
`lowering/rename_ledger.rs`'s module doc is the architecture reference.
Ideas it unlocked, still open:

- **Id-keyed rename executor.** Seal output is still projected to bare
  syms (`SealedRenames::*_by_name`) because the application visitors are
  string-keyed. Deleting the projection requires (a) emitting
  import/export decls under real syntax contexts instead of
  `Ident::new_no_ctxt` — today a single rename must hit both a no-ctxt
  emitted import local and the real-ctxt body references, which only
  sym-keyed application can do — and (b) hygiene-resolving the
  `Function`-scope heuristic sources (currently keyed
  `(sym, SyntaxContext::empty())`). Until then the by-name projection is
  the executor boundary; its two-contexts-one-sym assert is the tripwire.
- **Aggressive auto-naturalization.** Now safe to build: every rename
  flows through one seal, so a new auto-naming heuristic contributor
  (readable names for still-minified bindings, driven by the
  unrenamed-symbol priority-queue side output) only needs to submit
  intents plus deriving-subtree facts — conflict resolution, occupancy,
  and capture validation come for free, and the
  renamed-it/didn't-notice miscompile class (#2045) is unrepresentable.
- **Type-level structural-move barrier.** The "no structural moves
  between seal and execute" contract is convention-held; making
  non-execute passes take `&Module` would let the compiler enforce it.

## CLI usability

CLI usability and scripting-safety findings live in <CLI_DOGFOOD.md>.
