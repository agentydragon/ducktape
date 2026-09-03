# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Current AI-worker priority queue (2026-07-07)

This file is the dispatch queue, not a design record or changelog. Detailed
plans and evidence live here:

- <plans/selector_constraint_model.md> — P0 selector solver design, gates, and
  real-spec evidence queue.
- <plans/automated_spec_workflows.md> — automation-first CLI/workflow design.
- <SELECTOR_BUGS.md> — matcher/diagnostic bugs with anonymized examples.
- <ARCHITECTURE_BACKLOG.md> — deeper refactors, urgent only when they block this
  queue.
- `perf/` — measured performance notes. Update from real profiles before major
  matcher/index rewrites.

Planning hygiene: keep active dispatch order here. When a plan's core work is
complete, summarize only its remaining tail here instead of leaving the plan as
a second priority queue.

### P0 — single constraint-program resolver cutover

Detailed design and gates live in <plans/selector_constraint_model.md>. Current
dispatch summary:

1. Lower `identifiers: alpha_all` and retained hole/predicate forms into native
   selector constraints.
2. Keep exact target assignment owned by `CompiledSelectorProblem` +
   OR-Tools CP-SAT or a measured SAT fallback with semantic `all_different`.
3. Make production `source_match` materialization native-first, retire the
   legacy projection path that still constructs `ChunkResolver`, and move
   codemods, synthesis, and repair/prove gates onto one solver-backed selector
   semantics path. `match-selector` already uses `selector_runtime` for its
   baseline solve.
4. Fold staged relational vocabulary (`cross_ref`, `reads_member`,
   `member_of_module`, `passed_to_call`, `makes_decorate_call`,
   `intrinsic_alias`) into IR atoms or derived predicates over owner/reference
   plus AST facts.
5. Keep unsupported selector forms fail-closed after the projection fallback is
   retired; do not add a permanent procedural fallback.

Interactive agent-facing commands should target under 10 seconds on warmed
inputs for the largest known downstream specs. Anything over 60 seconds is a
workflow blocker unless the command is explicitly an offline/profile mode with
progress output and a resumable or cacheable plan.

### P1 — automation product flows over the solver

1. **Patch-plan based bulk codemods.** Extend `debundle spec selector-codemod`
   or add adjacent verbs so every broad rewrite can emit a dry-run patch plan,
   apply with filters, and explain every skipped candidate. The prove gate
   should be solver categoricity, not an independent selector-matcher path.
2. **Selector diagnostics — solver-backed replacement.** The keep-going JSON
   report (`debundle spec validate --keep-going --format text|json|ndjson`)
   landed (#2302; shared contract in `selector_diagnostics.rs`) and classifies
   unresolved / ambiguous / duplicate-claim failures with full provenance.
   Treat that as the current user-facing contract, not as architecture to carry
   forward unchanged: the new backend should emit per-target solver
   explanations directly. Fold remaining anonymous-statement failures, blocker
   comments, free-readable-identifier cases, and nearest-candidate needs into
   that solver-backed report shape.
3. **Spec repair from diagnostics.** Add a workflow that consumes the
   solver-backed keep-going report, proposes mechanically proven patch plans for
   no-match, ambiguous, duplicate-claim, and unsupported-selector cases, and
   leaves residual semantic decisions as explicit tasks.
4. **Orthogonal CLI surface.** Converge new automation on the
   inventory/plan/apply/validate/explain model in
   <plans/automated_spec_workflows.md>. Avoid one-off command shapes that cannot
   pipe a dry-run plan into review, apply, validation, and repair.
5. **Workflow latency budget.** Interactive commands target <10s on warmed
   inputs; >60s is a blocker unless explicitly an offline/profile mode with
   progress output and a resumable plan. The whole-spec minimize budget and the
   measured real-chunk numbers live in <debug/selector_minimizer_dogfood.md>.
6. **Version-port workflow.** Given v1 chunks + spec and v2 chunks, resolve v1
   selectors to source identities/fingerprints, search v2 for matching
   entities, apply confident selector repairs, and emit a residual report for
   semantic drift.
7. **New-app spec bootstrap.** Connect module proposals, naming output, and
   selector synthesis so new debundle specs start with structural selectors and
   an explicit debt/confidence report.
8. **Selector-debt ranking improvements.** Extend `debundle spec selector-debt`
   with source-aware ranking for multi-statement windows, repeated selector
   bodies that can become binding groups, and "stable literal by value"
   candidates. Prefer output that can feed the solver-backed patch-plan dry-run.
9. **Cross-module binding-group design.** Design a form for one matched source
   context to export bindings into different logical modules without duplicating
   the selector body.
10. **Free-readable-identifier diagnostics.** When an `alpha_all` selector uses
    readable names that are free references rather than local binders, explain
    that they do not refer to previously exported symbols. Suggest grouping or
    holes.
11. **Duplicate-claim identity.** Track claims by declaration identity instead
    of only emitted/minified spelling; include declaration kind and source
    location in duplicate-claim diagnostics.
12. **Public real-bundle smoke.** Build the Excalidraw live-browser smoke so
    private-corpus debundler issues can be reproduced and protected in public CI.
13. **Ground selector-stabilization skill fixtures.** The `debundle_stabilize`
    loop and playbook landed; add tested, anonymized fixtures for the common
    anchor-choice cases so the skill's guidance is executable rather than only
    prose.
14. **Port-based selector-stability evaluation.** Run a two-version bundle pair
    as a held-out evaluation of `debundle_stabilize`: report survived/broke
    verdicts by anchor kind and feed the scorecard back into the playbook.

### P2 — pipeline performance and architecture cleanup

1. Add `debundle run --reports=<list>` so dry-run/spec-check workflows can skip
   expensive reports they do not need.
2. Add chunk-level incremental rebuilds keyed by upstream bytes, spec slice,
   and Ducktape version.
3. Add an AST-hash SWC codegen cache for unchanged post-lowering modules.
4. Replace `JsChunk::{get_file,get_file_mut,remove_file}` linear scans with a
   path-keyed index if fresh profiles show chunk file lookup hot.
5. Move `split_entry_body` to a draining/move-based implementation if fresh
   profiles show retained-statement cloning hot.
6. Replace diagnostic-only matcher mirrors with solver-native explanations:
   generic `NoMatch` fallback reporting, empty `nearest_candidates`, fact
   near-miss/source-aware debt scoring, `match-selector` slack relaxation, and
   selector-IR row/stat stderr diagnostics.
   Keep cheap wrappers over production data; remove side data structures that
   exist only for the old matcher/row-solver path.

### P3 — read-off minimizer polish

The read-off minimizer's completed design and research notes were pruned from
`plans/` on 2026-06-22. The live maintenance tail is:

1. **Dogfood-apply on gaffer-private.** Run `synthesize-selectors --apply` on
   the real spec to convert the large set of fragile name-pins into robust
   `source_match` selectors, review for over-pin, and PR the beneficial ones.
   Revert any converted selector whose `match` block is >40 lines and has <=2
   holes back to a name pin. Keep pin-compatible with the released debundler
   expected by the gaffer validation flow, regenerate goldens, and re-measure
   selector debt after each batch.
2. **Retire the keep-shallow group cover.** Multi-target var binding-group
   read-off landed, but `minimize_var_group_selector` still falls back to
   `collect_expr_anchors` plus `AnchorCandidates` for groups whose per-slot
   single-binding view cannot single a slot out. Remove that path after
   tuple-aware read-off covers the residual groups, or deliberately accept them
   as selector debt.
3. **Hole declaration neighbors in enclosing-context anchoring.** When
   `render_via_neighbor_context` anchors a near-duplicate target to a stable
   neighboring function/class declaration, it still pins the neighbor's body
   verbatim. Reuse the per-form read-off to hole the neighbor name/params/body
   and keep only its discriminating value anchor. Ignored expectation fixture:
   `neighbor_context_whole_function_neighbor`.
4. **Route class-expression initializers through class read-off.**
   `try_var_read_off` still has no `Expr::Class` arm, so `const X = class {…}`
   can be pinned whole while the equivalent class declaration minimizes via
   class-body holing. Ignored expectation fixture:
   `class_expression_const_whole_body`.
5. **Journal `AlphaMatchScope` and reduce prove-gate fan-out.** Whole-spec
   apply spends too much time cloning alpha scopes during matcher backtracking.
   Replace clone-on-snapshot with an undo log, switch the alpha maps to
   `FxHashMap`, and use the candidate-index intersection to prune neighbor/group
   prove-gate fan-out. Profile evidence lives in
   <debug/selector_minimizer_perf.md>.

## Code refactor / dedup opportunities

Production-code (non-test) dedup/cleanup options, calibrated by
(LOC saved × safety). Closed refactors live in git history; this section
tracks only remaining options.

**Structural findings (full-package review):**

1. `realizability/mod.rs` — extract `gate_perf_counters` (~490-line `pub mod`).
   Entangled with index internals (`use super::*`, `pub(super)` recording APIs
   called from `RealizabilityIndex` / `IncrementalQuotient` query methods, and
   the timing-only `IncrementalQuotient::base_snapshot_stale` shadow state); a
   clean move needs a narrow recording trait first, not just a file move.
2. `vendor/mod.rs` further split (~1.3k lines + tests remain after the
   emission/manifests/passthrough/plan/strip/validate/wrappers extraction):
   package/subpath resolution helpers, export-surface collection,
   `MaterializedOutputChunkIndex`, the shared import factories
   (`DeferredImport` / `IdentRewriteTarget` / `PartialSwapIdentRewriter` and
   the `make_*` constructors), and the post-strip consumer scan are each
   liftable.
3. Two parallel top-level fact extractors:
   `program_analysis.rs::analyze_program_shallow` keeps its own traversal and
   `classify_top_level_decl` alongside the `facts/` walk; the two rule sets
   can drift independently. Fold the shallow extractor into the facts
   traversal or derive its records from `StatementFacts`.
4. `lowering/lower.rs` — extract the remaining inline phases of `lower_chunk`
   (naturalization, disambiguation, import planning, the per-module loop);
   each needs substantial captured state from `LowerChunkInputs` (15–20
   fields). Related: `lowering/mod.rs` carries a ~95-line import block from
   wildcard `use super::*` in every sub-module.
5. `output_layout.rs` — replace the 10 identical `self.root.join(CONSTANT)`
   accessors with a data-driven `report_path(name)` plus constants.
6. Encapsulation/type design: BTree collections in hot-path graph structures
   (`rollback_graph.rs`, `artifact.rs`, `realizability/`) where hash-based
   would be measurably faster — document determinism where it is required;
   make `DepKind`'s constraining vs non-constraining axis
   (`constrains_init_order()`) a first-class type distinction; the three-layer
   edge representation (domain graph → rollback graph → realizability index)
   has fragile bridging; `pub(super)` blankets `lowering/` field and function
   visibility; `SourceImportResolution = Option<(String, String, String)>`
   (`plan_references.rs`) needs a named struct.
7. Tests: `e2e/comma_list_owner_split_test.rs` asserts emitted shapes via
   whitespace OR-chains — parse or normalize instead;
   `peel/quotient_integration_test.rs` references share too much code with the
   system under test (most verdicts compare against the kernel's own
   `project_partition`; only `replay_partition` rebuilds independently, and
   compares only `cycle_set()`), and randomized merge/partition sequences and
   gate-residual promotion transitions are uncovered.
8. `ChunkBundle` ownership ping-pong through every stage
   (`artifact = result.artifact`) — cosmetic now that each stage is a pure
   function.

SWC-reuse evaluations (what to adopt, what was rejected and why):
<docs/swc_reuse.md>.

**Real value but needs design work / behavior-risk:**

1. Parameterize the per-form AST holing visitors (`render.rs` `hole_expr` /
   `hole_stmt`, `minimize/class.rs` `hole_class_member`, etc.) behind a `Holer`
   trait or table to collapse repeated per-variant match clusters. ~150 LOC,
   medium risk (over-abstraction hazard; the per-form holing strategies differ
   for good reasons).
2. Rename `ids.rs::LogicalModule` → `LogicalModuleIr` so it no longer clashes
   with `spec::LogicalModule`. The two are distinct types (IR materialization
   record vs. spec authoring input) that share a name purely historically,
   forcing qualified-path imports wherever both are visible. Ripples widely —
   update every reference under `lowering/`, `pipeline.rs`, and the e2e
   fixtures; the rename is mechanical but touches many files, so it was
   deferred out of the `MaterializeLogicalModulesOptions` embed PR.
3. Migrate `e2e/vendor_swap_test.rs` off raw `serde_json::json!` vendor-mark
   literals onto the typed vendor-mark builders. The raw-`json!` form bypasses
   `FixtureOpts` and the typed `VendorResolutionPlan` constructors, so test
   fixtures can drift from real config shapes without a compile error
   (e.g. a renamed vendor-mark field stays green in tests while breaking real
   specs). Points: <e2e/vendor_swap_test.rs> (~lines 1680, 1821 and the
   `report_out_dir` literals), builder surface in `vendor.rs`.
4. Consolidate the three `*BindingProjection` enums
   (`selector_constraint_backend.rs`, `selector_ir_solver.rs`,
   `selector_constraint_model_builder.rs`) into one shared projection type.
   They are structurally identical views of the same binding-namespace
   partition, duplicated per solver stage; the copies drift silently when a
   new binding kind is added. Unify behind one enum (plus any stage-specific
   extension) and re-point the three stages at it.

**Organization only (≈0 LOC removed, navigability win):** split the giant
files by responsibility — <selector_codemod.rs> (2.8k), <peel/quotient.rs>
(2.5k), <lowering/rename_ledger.rs> (1.7k).

**Evaluated and declined:** CLI common args via clap `#[command(flatten)]`. The
recurring flags (`--modules` / `--source-root` / `--format`) occur in
incompatible combinations across the `Args` structs with inconsistent attrs
(`source-root` carries `env` on some structs, not others; `MatchSelector` /
`Describe` / `ShowSource` have no `--modules`), so there is no cohesive group to
extract. Flattening `{modules, format}` would add `args.common.*` indirection
for a semantically-incohesive bundle (input locator + output format) without a
real clarity or LOC win. `peel`'s `CommonArgs` (`{graph, modules}`) stays as the
one cohesive case.

**Defer (high risk):** unifying the union-find / Tarjan-SCC / incremental
cycle-detection between <peel/quotient.rs> and
<realizability/condensation_order.rs>. They look parallel but encode different
correctness invariants for the realizability gate; a shared impl risks hiding
drift. Audit before attempting.

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

- **Contextual selectors.** The disambiguation _capability_ for
  helper-boilerplate that appears multiple times has landed (#2315): the
  minimizer reads off a stable immediate neighbor's unique anchor and
  emits a 2-statement-window `source_matches[]` claim rather than a copied
  overlapping selector body (the matcher already rejects ambiguous windows via
  the prove-gate). Decorator-helper neighborhoods — a helper
  declaration syntactically identical across many classes, disambiguated by
  an adjacent decorator call whose property strings identify the target —
  are the motivating shape. Remaining: (a) readable `before` / `after` /
  `near` _sugar_ so a hand-authored selector can express the same window
  without spelling out both statements; (b) non-adjacent / enclosing-call-site
  context (synthesis reads only the immediate ±1 neighbors today).
- **Constrained hole matching.** Multi-hole selectors need cheap local
  constraints so authors can say that a hole appears only as a specific
  argument, callback body, object property value, or statement-list slot. This
  should avoid scanning unrelated subtrees while preserving the current rule
  that ambiguous matches are hard errors.
- **Cross-module source claims.** Current `source_matches[]` entries export
  multiple bindings into one logical module. Add or design a form for one
  matched declaration context whose bindings should land in different modules,
  without repeating the whole source selector.

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

`lowering/rename_ledger.rs`'s module doc is the architecture reference for
the collect → seal → execute-once `RenameLedger` pipeline. Ideas it unlocked,
still open:

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

Open usability and scripting-safety findings from exercising the documented
workflows against a real spec; resolved items are deleted. Corpus-specific
paths and owner ids belong in the consuming repo.

- **`tana/re/web/AGENTS.md` BIN path stale** (gaffer-private): says
  `BIN=bazel-bin/external/ducktape_debundle_bin/file/debundle`; the actual
  path now carries a `+_repo_rules+` prefix. Fix in gaffer-private.

### Planner CLI follow-ups

Generic usability follow-ups for the top-level planner commands.
Corpus-specific paths and owner ids belong in the consuming repo.

- **Selector synthesis filters apply too late.** A downstream large-spec
  dogfood run of `debundle spec synthesize-selectors --rewrite
name-binding-to-source-match` showed that even one explicit
  `--item module:export` scanned every module file and member in the spec:
  `files_scanned=1745`, `modules_scanned=1745`, `members_scanned=6692`,
  `name_binding_members=1`, `elapsed=3.43s`. Scoped `--module-prefix` dry runs
  timed out at 30s CPU-bound. Explicit item batches were useful but still paid
  full-scan cost: top-100 items took 16.37s for 75 candidate changes; top-200
  took 31.38s for 157 candidate changes. Apply item/file/module filters before
  full YAML traversal and before source candidate generation where possible.
- **Selector synthesis apply emits non-reviewable YAML churn.** The same
  downstream dogfood run applied a top-100 item batch with 75 changed
  candidates. Selector correctness looked promising, but the YAML application
  path rewrote unrelated text: 13 files changed with 7331 insertions and 4273
  deletions, one large module accounted for most churn, and an unrelated
  top-level comment was dropped. Source-aware selector synthesis needs a
  text-preserving patch path for member selector replacement and
  binding-group/member collapse before broad generated patches are reviewable.
- **Selector synthesis needs a minimization acceptance loop.** A generated
  selector that exactly copies today's large function body, object literal,
  argument list, or class body can match uniquely while still being fragile
  spec debt. Dry-run/apply output should surface when a
  candidate is long/exact and should either minimize it automatically with
  `ANYTHING`, typed holes, `STMT_LIST`, or `DECLARATORS`, or emit a stable tooling-gap diagnostic that agents can route
  instead of hand-maintaining the exact body.
- **Diagnostics toggle for `modules propose`.** `--limit` now bounds
  proposals and diagnostics and the `limits` summary reports totals
  when details are truncated, but there is still no explicit
  diagnostics on/off toggle for first-pass planning, where proposal
  rows are the only thing the caller wants.
- **Concise explain mode.** Proposal/diagnostic structures on
  `describe` are already opt-in (`--include-proposals`), but there is
  still no compact mode focused on: selected owner identity and source
  span, atomic-unit membership, matching proposal (if any), immediate
  constraining neighbors, and the exact reason the owner is not
  landable today.
- **Source roots.** `show-source --source-root ...` depends on the
  consuming target's source-tree layout. Runbooks and skills should make
  that target-specific root explicit instead of assuming repository root
  or working directory.
- **Patch-plan naming.** `coverage` is useful for intersecting existing
  module YAML with atomic-unit coverage, but it is not the only way to
  discover readable work: `atoms --readable-only` and `modules propose`
  may show graph-valid work even when no whole patch section is ready.
  Docs and skill text should reserve "plan" for proposed edits that can be
  reviewed/applied, and avoid implying that empty coverage output means there
  is no landable work.
