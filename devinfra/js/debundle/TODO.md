# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Current AI-worker priority queue (2026-06-14)

This queue captures the active debundle tooling program. Treat this file as
the current priority source of truth; detailed design for the automation-first
direction lives in <plans/automated_spec_workflows.md>. Other tracking files
hold category-specific evidence:

- <CLI_DOGFOOD.md> — command UX and scripting-safety gaps found while running
  real workflows.
- <SELECTOR_BUGS.md> — selector matcher bugs and diagnostics gaps, with
  generic/anonymized examples.
- <ARCHITECTURE_BACKLOG.md> — deeper internal refactors, only urgent when they
  block the active workflows here.
- <perf/> — measured performance notes. Update these from actual profiles
  before major matcher/index rewrites.

Prefer dispatching work in this order. Large downstream spec migrations should
lean on tooling generated from this queue instead of hand-authored YAML.
Interactive agent-facing commands should target under 10 seconds on warmed
inputs for the largest known downstream specs. Anything over 60 seconds is a
workflow blocker unless the command is explicitly an offline/profile mode with
progress output and a resumable or cacheable plan.

### Live plan docs (debundle planning index)

One-line status for each `plans/` design doc; this is the discovery index.

- <plans/readoff_minimization.md> — **active.** Read-off selector minimizer
  (chunk-wide AST-shape index). Layer-1 index, function/object/class/var read-off,
  key-set minimization, adjacent-function grouping, prove-gate-via-index,
  whole-spec perf validation (W4), and the branch-and-bound cover deletion landed;
  multi-target var binding-group read-off, general co-occurrence grouping, and
  whole-body interior holing open. Holds its own current-state + backlog.
- <plans/readoff_algorithm_research.md> + <plans/readoff_research/> — **reference
  (complete).** Literature spike that gates the read-off design; durable, not a
  TODO.
- <plans/automated_spec_workflows.md> — **active.** North-star for the
  inventory/plan/apply/validate CLI surface and the synthesize / stabilize /
  version-port / new-app-bootstrap flows. Foundational milestones realized by the
  read-off work; repair-report, version-port, and bootstrap flows not started.
- <plans/adopt_names_via_bijection.md> — **not started.** Expose the `source_match`
  identifier bijection so one selector both locates a declaration and adopts
  readable names onto its params/locals/nested bindings.
- <plans/factor_vocabulary_rename.md> — **not started.** Rename "factor"
  vocabulary to graph-theoretic names (`OwnerGraph` / `AtomicDAG` / `ModuleDAG` /
  `ModuleAssignment`); atomic ducktape + gaffer-private cutover.
- <x/graph_planner_factorization.md> — **active (scratch).** Graph-derived module
  planner design space + algorithm/analysis backlog behind `debundle modules
propose`.

### P0 — automation-first selector workflows

1. **Forward-compatible minimized selector synthesis + shared source index.**
   Owned by <plans/readoff_minimization.md>: the read-off AST-shape index
   (`shape_index.rs`, superseting `selector_candidate_index.rs`),
   function/object/class/var read-off, literal/regex anchors, hole-based
   minimization, the prove-gate perf fix, whole-spec validation, and the
   branch-and-bound cover deletion have landed; multi-target var binding-group
   read-off, general co-occurrence grouping, and whole-body interior holing are in
   that plan's backlog. Don't duplicate its design or status here.
2. **Patch-plan based bulk codemods.** Extend `debundle spec selector-codemod`
   or add adjacent verbs so every broad rewrite can emit a dry-run patch plan,
   apply with filters, and explain every skipped candidate. (Selector
   minimization at synthesis time, unique-literal-initializer → structural
   selectors, and over-pinned-object `OBJECT_PROPS` rewrites are done via the
   read-off path; converting repeated member-form selectors into
   `binding_groups` is the co-occurrence-grouping item in the read-off backlog.
   The open work here is the dry-run / patch-plan / explain-every-skip
   infrastructure that the rewrite classes pipe through.)
3. **Selector diagnostics as machine-readable reports.** Emit a keep-going
   JSON report for unresolved selectors, ambiguous selectors, duplicate claims,
   and blocker comments. Include module path, export name, selector kind,
   target binding, first mismatch, nearest candidates, and recommended next
   action. This lets coordinators batch failures instead of scraping logs.
4. **Spec repair from diagnostics.** Add a workflow that consumes the keep-going
   report, proposes mechanically proven patch plans for no-match, ambiguous,
   duplicate-claim, and unsupported-selector cases, and leaves residual semantic
   decisions as explicit tasks.
5. **Orthogonal CLI surface.** Converge new automation on the
   inventory/plan/apply/validate/explain model in
   <plans/automated_spec_workflows.md>. Avoid one-off command shapes that cannot
   pipe a dry-run plan into review, apply, validation, and repair.
6. **Workflow latency budget.** Interactive commands target <10s on warmed
   inputs; >60s is a blocker unless explicitly an offline/profile mode with
   progress output and a resumable plan. The whole-spec minimize budget and the
   measured real-chunk numbers live in <plans/readoff_minimization.md> (W4) and
   <debug/selector_minimizer_dogfood.md>.

### P1 — broad workflow integration

1. **Version-port workflow.** Given v1 chunks + spec and v2 chunks, resolve v1
   selectors to source identities/fingerprints, search v2 for matching
   entities, apply confident selector repairs, and emit a residual report for
   semantic drift.
2. **New-app spec bootstrap.** Connect module proposals, naming output, and
   selector synthesis so new debundle specs start with structural selectors and
   an explicit debt/confidence report.
3. **Selector-debt ranking improvements.** Extend `debundle spec selector-debt`
   with source-aware ranking for multi-statement windows, repeated selector
   bodies that can become binding groups, and "stable literal by value"
   candidates. Prefer output that can feed the P0 codemod dry-run.
4. **Cross-module binding-group design.** Design a form for one matched source
   context to export bindings into different logical modules without duplicating
   the selector body.
5. **Free-readable-identifier diagnostics.** When an `alpha_all` selector uses
   readable names that are free references rather than local binders, explain
   that they do not refer to previously exported symbols. Suggest grouping or
   holes.
6. **Duplicate-claim identity.** Track claims by declaration identity instead
   of only emitted/minified spelling; include declaration kind and source
   location in duplicate-claim diagnostics.
7. **Public real-bundle smoke.** Build the Excalidraw live-browser smoke so
   private-corpus debundler issues can be reproduced and protected in public CI.

### P2 — pipeline performance and architecture cleanup

1. Use actual profiles to prioritize selector matching/index work. The expected
   direction is one parse/index per chunk, prepared selector reuse, inverted
   indexes for stable anchors, and memoized selector-body resolution; validate
   each major change in <perf/>.
2. Add `debundle run --reports=<list>` so dry-run/spec-check workflows can skip
   expensive reports they do not need.
3. Add chunk-level incremental rebuilds keyed by upstream bytes, spec slice,
   and Ducktape version.
4. Add an AST-hash SWC codegen cache for unchanged post-lowering modules.
5. Replace `JsChunk::{get_file,get_file_mut,remove_file}` linear scans with a
   path-keyed index if fresh profiles show chunk file lookup hot.
6. Move `split_entry_body` to a draining/move-based implementation if fresh
   profiles show retained-statement cloning hot.

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

## Structural selector language

Detailed workflow priorities live above and in
<plans/automated_spec_workflows.md>. Remaining selector-language gaps should be
implemented only when they unlock synthesis, stabilization, repair, or porting
workflows, and must use generic synthetic fixtures.

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
