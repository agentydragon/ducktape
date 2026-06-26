# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Current AI-worker priority queue (2026-06-23)

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

Planning hygiene: keep active dispatch order here. `plans/` files are design
records and scoped backlogs; when a plan's core work is complete, its remaining
tail should be summarized here instead of leaving the plan looking like a second
priority queue.

**Current focus (2026-06-26).** PRs #2439, #2443, #2446, and #2447 moved
`source_match` onto the Ascent-backed selector path, lowered the exact
single-statement AST subset, stopped calling `ChunkResolver` for native
selectors, and added native class-superclass constraints. That proved the
global-resolution direction, but profiling the production-sized path showed the
current Ascent exact-assignment encoding is not the endpoint: it carries
`AssignmentRow` payloads through `partial_assignment` / `stepped_assignment`
relations and implements target injectivity as pairwise row filtering instead
of a native global constraint. The remaining P0 is to carve a
`SelectorConstraintModel` boundary, keep Ascent/Rust as fact and allowed-tuple
derivation where useful, and move exact target assignment to a CP/SAT backend
with first-class `all_different`. Unsupported forms still fail closed instead
of taking a production procedural fallback.

Dispatch work in this order:

1. **Constraint-model/backend pivot (P0.0).** Introduce the explicit
   `SelectorConstraintModel` contract: typed finite domains, allowed tuples,
   equality/disequality, ordering, target projection, and semantic
   `all_different`. Target OR-Tools CP-SAT first; use RustSAT + CaDiCaL/Kissat
   only if OR-Tools integration is too expensive. Do not optimize the current
   `AssignmentRow` scheduler as if it were the final solver.
2. **Alpha-all as query structure (P0.1).** Lower `identifiers: alpha_all` to
   logic variables, equality, disequality / `all_different`, and scope facts.
   Do not clone the procedural `selector_match::Bindings` matcher inside the
   solver; alpha-renaming should fall out of the query.
3. **Core hole predicates (P0.2).** Lower simple `ANYTHING` / `EXPR` / `STMT`
   holes, regex string predicates, and then ordered run holes (`STMT_LIST`,
   `OBJECT_PROPS`, `DECLARATORS`, `ARGS`, `CLASS_REST`, `CASE_REST`,
   `ARRAY_ELEMENTS`) as native constraints. Preserve the fail-closed rule:
   unsupported constructs report `unsupported` until their faithful encoding is
   implemented.
4. **Source-match surface pruning (P0.3).** Keep unused selector/tooling
   options out of the native IR. The known unused surfaces
   (`target_statement`, `target_statements`, authored `wildcard_string_literals`,
   the single-choice `match-selector --identifiers` flag, and the exact-body
   selector-codemod fallback knobs) have been removed. Remaining pruning should
   target tooling-only conveniences, not selector semantics that Gaffer still
   needs.
5. **Derived relational predicates (P0.4).** Fold the remaining bridge
   vocabulary (`cross_ref`, `reads_member`, `member_of_module`,
   `passed_to_call`, `makes_decorate_call`, `intrinsic_alias`) into IR atoms or
   derived predicates over owner/reference + AST facts.

The minimizer polish tail and automation product flows remain valuable, but
they should build on this single constraint-program resolver contract rather
than harden today's fallback oracle or late-bridge shape.

Prefer dispatching work in this order. Large downstream spec migrations should
lean on tooling generated from this queue instead of hand-authored YAML.
Interactive agent-facing commands should target under 10 seconds on warmed
inputs for the largest known downstream specs. Anything over 60 seconds is a
workflow blocker unless the command is explicitly an offline/profile mode with
progress output and a resumable or cacheable plan.

### Live plan docs (debundle planning index)

One-line status for each `plans/` design doc; this is the discovery index, not a
parallel dispatch queue.

- <plans/selector_constraint_model.md> — **active (P0 global resolver).**
  Canonical plan for the selector model, backend ownership, execution phases,
  verification gates, and Gaffer evidence queue. #2439 closed the global-solver
  admission path for `source_match`; #2443/#2446/#2447 moved the exact native
  subset out of the oracle. Current top priority is the solver-backend pivot:
  preserve one whole-spec constraint model, but stop treating Ascent row
  enumeration as the exact-assignment backend. Next work is alpha-all and hole
  lowering into that model, plus pruning unused source-match surface before
  carrying it forward. The landed bridge primitives (`cross_ref`,
  `reads_member`, `member_of_module`, `passed_to_call`, `makes_decorate_call`,
  `intrinsic_alias`) are useful fact/selector vocabulary, but are bridge
  implementations until they fold into derived predicates. See
  <debug/2026_06_19_p4_debt_worklist.md> for real-spec evidence.
- <plans/automated_spec_workflows.md> — **active design, downstream of P0.**
  North-star for the inventory/plan/apply/validate CLI surface and the
  synthesize / stabilize / version-port / new-app-bootstrap flows. Foundational
  milestones realized by the read-off work; repair-report, version-port, and
  bootstrap flows should consume the solver-backed validation/diagnostic
  contract rather than cloning selector resolution logic.
- <plans/adopt_names_via_bijection.md> — **not started.** Expose the `source_match`
  identifier bijection so one selector both locates a declaration and adopts
  readable names onto its params/locals/nested bindings.
- <plans/factor_vocabulary_rename.md> — **not started.** Rename "factor"
  vocabulary to graph-theoretic names (`OwnerGraph` / `AtomicDAG` / `ModuleDAG` /
  `ModuleAssignment`); atomic ducktape + gaffer-private cutover.
- <x/graph_planner_factorization.md> — **active (scratch).** Graph-derived module
  planner design space + algorithm/analysis backlog behind `debundle modules
propose`.

### P0 — single constraint-program resolver cutover

Detailed design and gates live in <plans/selector_constraint_model.md>; keep
this list as the dispatch summary, not a second plan.

1. **Wire the exact-assignment backend.** The backend-neutral
   `SelectorConstraintModel`, backend problem contract, and backend solver
   adapter are landed, and OR-Tools CP-SAT now builds under RBE with
   `all_different`/table-constraint smoke coverage. Next, convert the typed
   Rust backend problem into the CP-SAT sidecar wire format, run it through the
   backend adapter, and make the anonymized broad-vs-specific fixture resolve
   through CP-SAT rather than `AssignmentRow` enumeration.
2. **Keep Ascent on fact/table derivation, not exact assignment.** Ascent may
   continue deriving relation support and allowed tuples, but exact target
   assignment must be owned by OR-Tools CP-SAT or a measured SAT fallback with
   semantic `all_different`.
3. **Lower alpha-all declaratively.** Represent selector-local identifier
   bindings/references as variables and constraints over facts, including
   equality, disequality / `all_different`, and scope. This is the blocker for
   current gaffer-private payoff: its Tana spec uses `identifiers: alpha_all`
   for every `source_match`.
4. **Lower the retained hole vocabulary.** Start with simple single-node holes
   and regex string predicates, then add ordered run-hole placement for the
   high-volume families (`STMT_LIST`, `DECLARATORS`, `OBJECT_PROPS`). Each
   lowering must be faithful or fail closed.
5. **Prune unused tooling options before nativeizing them.** The current
   Ducktape/Gaffer census no longer has public `wildcard_string_literals`,
   `target_statement`, `target_statements`, `match-selector --identifiers`, or
   selector-codemod exact-body fallback surfaces to carry forward. Source-aware
   `selector-debt` debug options should be trimmed once their solver-backed
   replacements are scoped. The retained `EXPR_*` / `STMT_*` / `STMT_LIST_*`
   forms are readability labels and run holes, not old `STATEMENT_*`
   compatibility spellings.
6. **Fold bridge vocabulary into derived predicates.** Re-express the staged
   relational selectors as solver predicates over owner/reference + AST facts.
   Real-spec Gaffer work should supply missing predicates and diagnostics, not
   another permanent resolver layer.

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

## Code refactor / dedup opportunities (2026-06-17 survey)

Production-code (non-test) dedup/cleanup options surfaced by a codebase survey.
Calibrated by (LOC saved × safety). Done so far: the `minimize/` single-pick
collapse (#2346), the CLI report-dispatch helper (`cli::emit_report`), the
`PurityReason` construction centralization (`PurityReason::new` +
`Purity::from_reason_opt_detail`), and the `vendor/mod.rs` boundary-mapping
collect+validate single-pass merge (`collect_and_validate_boundary_mapping`).

**Real value but needs design work / behavior-risk:**

1. Parameterize the per-form AST holing visitors (`render.rs` `hole_expr` /
   `hole_stmt`, `minimize/class.rs` `hole_class_member`, etc.) behind a `Holer`
   trait or table to collapse repeated per-variant match clusters. ~150 LOC,
   medium risk (over-abstraction hazard; the per-form holing strategies differ
   for good reasons).

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

## Cleanup backlog (post-#2398 review)

Items surfaced by the post-#2398 debundler cleanup review that were **not**
applied. The applied items (the empty-arm collapse, the `Resolution`
`single_from_map`/`unique_owner` helpers, `chunk_facts` `build_children_map`, the
`MemberRequest` `RelationalSelector` enum + `selector_kind_label`, and the
`resolve_anchor` anchor-resolution helper) are done and intentionally omitted.

- **C3 remainder — deeper data-driven relational resolution.**
  `lowering/materialize/plan_builder.rs` now shares the common relational
  member scan through `relational_targets`, so the repeated no-op guard and
  per-member loops are no longer open-coded six times. The deferred part is a
  fuller collapse of the six `resolve_and_claim_*` passes (`cross_ref`,
  `reads_member`, `member_of_module`, `passed_to_call`, `makes_decorate_call`,
  `intrinsic_alias`) into one data-driven pass. That remains behavior-risky:
  the passes have genuinely different resolution-builder signatures
  (`member_of_module` needs `import_sources`; others do not), different anchor
  sources (`resolved_anchor_bindings` vs `claimed_member_bindings` vs none),
  different per-primitive kernel calls and `with_context` closures, and each
  call site carries a distinct `time_phase!` timing label. If attempted,
  preserve every `time_phase!` label and keep the per-primitive bits legible
  (shared-helper route, not a code-gen macro). The per-resolver
  `#[allow(clippy::too_many_arguments)]`s only become removable once the
  standalone resolvers disappear into the loop.

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
  emits a 2-statement-window `source_match` + `target_binding` rather than
  a copied overlapping selector body (the matcher already rejects ambiguous
  windows via the prove-gate). Decorator-helper neighborhoods — a helper
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
- **Cross-module binding groups.** Current `binding_groups` export
  multiple bindings into one logical module. Add or design a form for
  one matched declaration context whose bindings should land in
  different modules, without repeating the whole source selector.

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
