# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Active work queue

This file is the dispatch queue, not a design record or changelog. Detailed
plans and evidence live here:

- <docs/selector_resolution.md> — how selectors resolve, and the measured reason
  the architecture is shaped that way.
- <plans/relational_selectors.md> — the remaining selector-language work.
- <plans/automated_spec_workflows.md> — single-input selector authoring and
  diagnostic workflows.
- <plans/module_proposals.md> — proposal metadata and granularity follow-ups.
- <plans/purity_analysis.md> — purity annotation and redundant-hint follow-ups.
- <plans/native_receiver_identity.md> — native receiver identity and its
  descriptor, alias, method-receiver and initialization proof obligations.
- <plans/convergence_landing.md> — how the semantic-convergence refactor
  (reference implementation: draft #14552) lands on `devel` in slices: debt
  inventory, target code shape, dependency waves, exit gates.
- <plans/abstract_interpreter.md> — migrate the bespoke semantic certificates
  onto one abstract interpreter, domain and builtin summary table.
- <plans/synchronous_execution_fallback.md> — make modeled effects authoritative
  for incomplete at-init calls before removing the graph builder's conservative
  read-closure fallback; reported havoc reach is not yet consumed.
- <ARCHITECTURE_BACKLOG.md> — unrelated deferred integrity and test/spec items.
- `perf/` — measured performance notes. Update from real
  profiles before major matcher/index rewrites.

Planning hygiene: keep active dispatch order here. When a plan's core work is
complete, summarize only its remaining tail here instead of leaving the plan as
a second priority queue.

### Remaining dispatch order

Safety evidence outranks speculative deletion counts. Source inspection is not
an end-to-end reproduction; performance candidates require fresh opt profiles.
The sections below hold details, not competing priority queues.

1. **Compositional semantic authority.** Follow the waves in
   <plans/convergence_landing.md>: Wave 0 (compile the reference, split the
   giant e2e targets), Wave 1 (independent slices under the legacy gate), Wave 2
   (kernel), Wave 3 (coverage producers), Wave 4 (discrepancy audit, flip,
   legacy deletion). AR1–AR4 close through those waves; AR7's soundness-contract
   investigation can proceed independently. Draft #14552 is the source to port
   from, not a branch to merge.
2. **Single-input spec assistance.** Keep selector synthesis and repair
   diagnostics scoped to one configured input program and one exact spec.
   Prioritize correct edits and explicit skipped-candidate reasons. Proposed
   selector edits are proved by the shared structural resolver; diagnostics
   remain suggestions, and the author decides what to apply. Keep
   gate-before-write, atomic writes and dry-run reporting.
3. **Public browser smoke.** The Excalidraw corpus and its accepted residual
   baseline landed (<excalidraw/README.md>). Remaining per
   <plans/excalidraw_live_smoke.md>: a realistic spec (vendor swap marks,
   source-anchored modules, renames), the live-browser smoke covering bundled
   vendor singleton/importmap behavior, and the public canonical-spec check.
   Node probes already run; browser loading is the missing contract, not a
   missing vendor schema.
4. **Profile-backed AST and lookup work.** Measure cloning, file-name lookup
   and alpha-binding snapshots before ownership redesign, indexes or undo logs.
   Preserve ordering/hygiene and the independent Node differential oracle.
   Anonymous uniqueness uses coarse semantic-token buckets with exact equality;
   profile real workloads before adding indexes or other machinery.
5. **Remaining selector authoring gaps.** Follow the selector diagnostics above
   and minimizer work below. Keep near-miss ranking heuristics distinct from
   boolean backtracking.
6. **Remaining responsibility seams.** Use AR4–AR5 and AR8 for bounded ownership
   and identity changes that simplify real callers. AR6 needs a lowering-design
   decision before implementation. Prefer deleting redundant setup over deleting
   behavioral assertions or adding test frameworks.

### Architecture review follow-ups

These IDs identify remaining work; dispatch order stays above. Existing plans
retain the detailed semantic designs. Preserve the ordinary realizability gate,
shared import order, explicit saved DTOs, and validated writer boundary.

- **AR1 — Compose operation effects and proof authority across consumers.**
  `SynchronousCalls` in <facts/statement_facts.rs> carries timing, lexical
  receipts, whole-owner completion, heap effects and classifier disagreement
  separately; <facts/purity_classification.rs> and
  <realizability/effect_translation_validation.rs> consume different judgments.
  Extend the shared operation transfers to compose effects, unresolved
  obligations and proof dependencies through calls and owner finalization;
  audit purity, graph, heap and imported-summary consumers together. Follow
  <plans/abstract_interpreter.md> and <plans/synchronous_execution_fallback.md>.
  Close when a real blocked extraction gains ordinary-gate acceptance and
  original/emitted runtime parity, with exact dependency and staged-replay
  controls across every affected consumer. Empty ledgers or timing completion
  cannot discharge uncovered domains; effect assessment remains report-only
  until its semantic and placement obligations are proved.

- **AR2 — Bound opaque callback analysis with composable summaries.**
  `invoke_callback_pool_once` in <facts/sync_effects/interpreter.rs> supplies
  other callbacks as possible arguments, evaluates against evolving state, and
  falls back at depth, expansion and pool limits. Reduce repeated cross-product
  exploration with proved callee/reach summaries and shared effect composition;
  keep global/unknown behavior explicit. Close with neutral CLI controls around
  the current limits, recursion, callback order and reentry, plus a real profiler
  comparison on identical inputs. Exhaustion must retain conservative effects,
  dependencies and precision-loss evidence. Increasing limits alone is not a
  resolution; the rejected global-prefix fixpoint in <docs/design.md> needs new
  evidence before reconsideration.

- **AR3 — Compose callable capabilities and initialization availability.**
  `CallableSummary` in <analysis_inputs.rs>, <cross_module_purity.rs> and
  `CallableAvailability` in <facts/sync_effects/execution_index.rs> distribute
  return, callback, capture and availability proofs across specialized paths.
  The isolated-initializer recognizer excludes providers with other statements.
  Reuse the shared interpreter to compose these independent facts through
  canonical import, alias, namespace and reexport identities; prove availability
  under emitted initialization order before broadening provider shapes. Close
  with local/imported equivalent-shape controls, initialized providers beyond
  isolated leaves, and mutation, capture and early-cycle negatives, preserving
  exact dependencies through saved analysis. Extend the lifetime obligations in
  <plans/native_receiver_identity.md>; new recognizers need explicit checked
  preconditions rather than declaration-form shortcuts.

- **AR4 — Give execution data, runtime state and orchestration separate owners.**
  The interpreter no longer nests under <facts/sync_effects/execution_index.rs>
  (#15073), and the kernel, `world/` and both `adapters/` are peers, but
  interpreter-read `ExecutionIndex` fields are `pub(super)` rather than an
  interface. <lowering/mod.rs> also prepares whole-program summaries and loads
  analysis. Extract immutable execution program/index interfaces. Put phase
  orchestration above those services; finish moving
  semantic records still owned by `reports/schema.rs` to domain owners, retaining
  <execution_evidence.rs> and report projections. Close when real callers use
  those interfaces without reverse ownership, with unchanged semantic facts,
  report/saved fields, proof provenance, gate results and staged replay. File
  moves alone do not establish that boundary.

- **AR5 — Reuse one prepared source/index across analysis phases.**
  <frontend_elaboration/elaboration.rs> and <staged_analysis/chunk_facts.rs>
  recollect structural facts; `reparse_unretained_entries` in
  <staged_analysis/program_summary.rs> rebuilds released syntax. Extend
  `SourceProgram`/manifest ownership so selectors, provider analysis and local
  facts reuse immutable syntax and indexes across the required provider closure.
  Close when those production paths neither reparse prepared entries nor build
  duplicate structural censuses, while exact source sites, repeated declarations,
  hygiene, provenance and staged replay remain stable. Preserve `SourceWithAst`
  mutation invalidation, its release at the `EmissionFiles` boundary, and actual
  source-marker/pass-through-byte controls. Profile retention and memory before
  claiming a performance improvement.

- **AR6 — Make the limits of source-order ESM lowering explicit.**
  <graph/edge.rs> encodes co-location for shared declarations and rejects writes
  across destinations because imported bindings are read-only. Distinguish a
  precision gap, a poor partition and a restriction of the supported lowering
  in gate explanations and neutral shared-cell examples. Close the design item
  with a documented choice: retain that strategy and explain its unsupported
  partitions, or specify another strategy's TDZ, live-binding, initialization,
  reentry and abrupt-completion obligations before implementing it. Any new
  strategy needs ordinary-gate and original/emitted differential controls under
  <docs/design.md>; weakening constraints cannot supply missing emitter behavior.

- **AR7 — Reconcile the documented soundness guarantee with executable behavior.**
  <docs/design.md> promises observational equivalence, but its "Side effects we
  can't see locally" section says accepted global-effect reorderings can change
  behavior. This is a contract inconsistency, not a reproduced new soundness
  bug. Check the claims against neutral CLI cases for global reads/writes,
  imported-object mutation and refused dynamic scope, including the relevant
  admitted host premises. Close by replacing stale claims with the verified
  scope and regression references, or repairing any reproduced unsafe acceptance
  conservatively before updating the contract. Expected rarity in bundled code
  is not a soundness condition.

- **AR8 — Separate qualified module identity from author spelling and output paths.**
  `ModulePath::parse` in <spec/spec.rs> folds case and accepts chunk-qualified
  spellings; `logical_module_path` in <selectors/resolution/selector_resolve.rs>
  and <lowering/materialize/plan_builder.rs> independently split qualified
  strings. Use a typed chunk-qualified logical reference between services and
  convert to author/wire spelling or physical output paths only at boundaries.
  Decide and document whether case folding is an authoring rule before changing
  accepted input. Close with all callers updated together and CLI/report controls
  for same-named modules in different chunks, case collisions, normalized paths
  and supported qualified spellings. Preserve Linux path semantics without
  introducing transitional identity shims.

### Automation product flows over the solver

Design and milestones: <plans/automated_spec_workflows.md> — single-input
selector synthesis, diagnostics and reviewed edits. Every candidate is scoped
to one exact source/spec pair and held to the interactive budget in
<docs/selector_resolution.md> § Interactive budget.

1. **Selector diagnostics.** Sharper first-mismatch reasons for `no_match`'s
   `nearest_unclaimed`: the first unmatched item of a multi-declaration range,
   the first incompatible identifier binding or sub-expression, a
   parameter-pattern mismatch, and list-hole binding spans; and near misses
   for multi-statement templates. Outcomes also do not yet record name-pin debt
   annotated with `note:`, or `alpha_all` readable names that are free
   references rather than local binders.
2. **Selector-debt ranking improvements.** Extend `debundle spec selector-debt`
   with source-aware ranking for multi-statement windows and "stable literal by
   value" candidates. Prefer output that can feed the patch-plan dry-run.
3. **Cross-module binding groups.** All captures listed under one
   `source_matches[]` entry's `bindings[]` list belong to the module containing
   that selector. Declarations present only in its `match:` context are not part
   of that ownership. Design a form for listed captures from one matched context
   to land in different modules, without repeating the selector body.
4. **Explain remaining relational contradictions.** Infeasible groups now
   report ownership witnesses from original candidate domains when available.
   For groups that need relational propagation to explain the failure, retain
   premises with domain reductions before attributing a smaller conflict.
   Assumption-core localization remains rejected for its measured cost
   (<docs/selector_resolution.md> § Unsatisfiable programs).

### Test infrastructure

1. **Public real-bundle smoke.** Build the Excalidraw live-browser smoke
   (<plans/excalidraw_live_smoke.md>) so private-corpus debundler issues can be
   reproduced and protected in public CI.
2. **Runtime resource controls.** `url_rebase_test` covers lazy JavaScript and
   stylesheet reads through `new URL(..., import.meta.url)` under Node. Still
   uncontrolled: URL search/fragment and encoded path identity, nested CSS asset
   references, real browser worker/style loading, dynamic consumers of package
   replacements, and the direct `Worker`-string rewrite (whose browser document
   base is not the module URL; the resource inventory marks it unresolved).
3. **Ground selector-stabilization skill fixtures.** Add tested, anonymized
   fixtures for the common anchor-choice cases of the `debundle_stabilize`
   playbook so its guidance is executable rather than only prose.

### Encountered tooling friction

- **F5 — Extend `spec lint --probe-analysis` to the remaining premise
  kinds.** The probe (SPEC.md § Spec lint) withholds one annotation from an
  in-memory spec copy and reruns the whole program on the prepared input;
  `purity: pure` hints use it. Add, one kind per change: `host_behavior` keys,
  `native_operation_inputs`, `native_call_identity_inputs`,
  `initializer_call_effect_inputs`, `trusted_dataflow_summaries`,
  `deferred_global_publications`, `program_premises`, `chunk_export_purity`.
  Each needs its withholding edit and any report that names the annotation by
  construction excluded from the comparison; anything else that differs means
  the annotation is used. Cost is one full run per probe: rerun only affected
  chunks, and run independent probes in parallel, once a measurement on a real
  spec shows it matters.
- **F3 — Summarize the complete gate inventory.** A canonical program had
  several rejected chunks while its pipeline stderr led with only the first
  failure. This caused an undercount until every saved rejection report was
  inspected. Add a bounded program-level summary with each processed chunk's
  outcome, rejection class, exact totals and report paths. Mark unprocessed
  chunks explicitly; the first fatal error is not the full inventory.
- **F4 — Explain rejected provider initialization closures.** The imported-array
  input diagnostic says only `complete acyclic ordinary static initialization
closure`, conflating unresolved host requests, missing captured chunks,
  top-level await, and real source cycles. Include a bounded witness with the
  first failing chunk/request or cycle path and the relevant host-admission
  decision. Preserve conservative rejection; do not suggest admitting an
  unknown package as a builtin. Close with neutral examples covering an
  unlisted Node builtin, an unknown package, and a real module cycle.
- **Distinguish uninitialized observations from lexical direct reads.** The
  graph keeps promoted uninitialized observations under its non-droppable
  `Direct` edge role, and `gate describe` labels them `direct at-init read`.
  This can send source review looking for a binding absent from the initializer.
  Retain an explicit diagnostic discriminator through graph/report and gate
  explanations while preserving the current ordering and projection rules.

- **F4 — Explain callback-fallback read provenance.** A pool-limit report names
  the pool size and promoted read without a path from an actual callback seed
  through the static source-owner closure. Add a bounded predecessor witness
  with callable creation/initialization availability and the first cache
  contribution's source owner. A later initializer may consume an earlier
  cached read; distinguish those locations. Preserve actual TDZ observations
  and conservative effects; an unavailable future function body is a diagnosis
  hypothesis until a neutral failing fixture establishes it.
- **F6 — Expose a read-only bulk resolved-claim inventory.** `coverage`
  resolves every non-empty module claim set in one joint call, but emits only
  module-level binding/atomic-group summaries (<peel/plan.rs>).
  `modules.json` describes materialized selected claims. Add a source-only,
  provenance-checked query such as
  `debundle claims inventory` that reports every authored claim in the scoped
  module trees, whether or not a later extraction selects it. Keep the
  physical YAML path separate from the normalized owner, and typed claim
  identity plus readable/template names separate from the actual raw binding.
  Include declaration source-unit IDs and parsed initializer form/span where
  available. Report resolution and ownership-selection statuses separately so
  resolved-but-unselected claims remain visible; do not filter them away.
  Do not prefilter to selected or near-complete owners: include claims from fully
  unselected and partially selected owners. Keep physical implicit placement
  distinct from explicit canonical claims; unrestricted `bindings` can still
  leave explicit anonymous-statement exclusions. Initializer shape is a lead,
  not eligibility evidence: source/provenance, native guards and author premises
  must qualify a match. Keep the combined ordinary gate and retention checks.
  Do not decode caches, scan source text manually, or add a legacy mapping
  fallback.
  Close when one query emits a deterministic row for every in-scope typed
  claim from one joint selector solve per chunk, retains ordinary
  graph/source/spec provenance checks, and remains available when the later
  placement gate rejects. Use neutral controls for use-site captures, direct
  pins, structural members, imports, anonymous claims, repeated declarations
  and path-case aliases; accepted rows must reconcile with `modules.json`,
  while unresolved statuses never infer identity by spelling.

### Pipeline performance and architecture cleanup

- **Keep resolved operation references out of semantic analysis identity.**
  `ChunkAnalysisInputs.initializer_call_effect_inputs` still serializes the
  authored `{module, name}` reference after frontend resolution has recorded the
  actual declaration in `AnalysisSourceSites`. Updating only that owner key
  therefore changes the analysis projection and requires fresh analysis. Project
  the operation's effect/provenance contract and resolved source identity into a
  selector-free analysis type; keep the ordinary joint selector and declaration
  validation before cache reuse. Owner-only edits should reuse matching facts,
  while changes to the selected declaration, source or effect contract still
  invalidate them.

Proposer-gate, `debundle run` (report opt-out, chunk-level incremental
rebuilds, codegen cache), and materialize-stage performance work lives in
<perf/proposer.md>.

1. `JsChunk::{get_file,remove_file}` (`artifacts/artifact.rs`) are linear
   scans over `files`, so passes that touch every file go O(n²) per chunk.
   Replace them with a path-keyed index if fresh profiles show chunk file
   lookup hot.
2. `split_entry_body` (`lowering/lower.rs`) borrows the prepared source AST.
   That AST is also consulted by `module_output::anonymous_statement_comments_by_span`
   and carries source maps/hygiene into output. A draining split is therefore
   an ownership-boundary change, not a local `iter()` → `into_iter()` edit.
   The residual-item helper now returns `Option<ModuleItem>` (no temporary
   result vector or fictitious error path). Profile the remaining required
   copies before redesigning source ownership.

### Read-off minimizer polish

1. **Dogfood-apply on the private downstream repo.** Run `synthesize-selectors --apply` on
   the real spec to convert the large set of fragile name-pins into robust
   `source_match` selectors, review for over-pin, and PR the beneficial ones.
   Revert any converted selector whose `match` block is >40 lines and has <=2
   holes back to a name pin. Keep pin-compatible with the released debundler
   expected by the downstream corpus validation flow, regenerate goldens, and
   re-measure selector debt after each batch.
2. **Undo-log alpha bindings in the matcher.** `selector_match`'s `Bindings`
   (a stack of `AlphaScope` frames of `HashMap`s) is cloned to snapshot before
   each backtracking alternative. If fresh profiles of whole-spec
   `synthesize-selectors --apply` show that cloning hot, replace
   clone-on-snapshot with an undo log and switch the maps to `FxHashMap`.
3. **Skip neighbor-borrowed uniqueness.** When a candidate's uniqueness comes
   only from a non-target neighbor (the target's own body fully holed, a
   neighboring declaration pinned), skip it with a reason instead of reporting
   `would_change`. Key the check on that property, not on selector length:
   neighbor-borrows as short as two lines slip under the >40-line over-pin
   heuristic.
4. **Say why a `--candidates N` menu is short.** `synthesize-selectors
--candidates N` returning one candidate does not say whether only one anchor
   exists or the menu is not enumerated for that shape (seen on an empty
   `class X extends Y {}`). Distinguish the two in the JSON.

## Purity classifier

Statement-level overrides, the redundant-hint guardrail and compositional proof:
<plans/purity_analysis.md>.

The ignored `inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle`
fixture is a deferred **RegExp admission feature**, not a contradictory classifier
bug. The `RegExp` row in `facts/abstract_domain/builtins.rs` throws on pattern
values, so purity needs static ECMA-262 pattern validation before admitting
literal `RegExp` construction; the active classifier test correctly expects
Unknown today. Keep the future test ignored until that precondition is
implemented; do not admit constructors that can throw.

## Rename pipeline

`lowering/rename_ledger.rs`'s module doc is the architecture reference for
the collect → seal → execute-once `RenameLedger` pipeline. Ideas it unlocked,
still open:

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

## Import re-export claims

Extend `resolve_imported_binding` in `lowering/imports/imports_runtime.rs` to
support default and namespace import member re-exports (<SPEC.md> § Entities).
Add neutral CLI/Node cases for both forms; a minimal default case is
`import a from "./dependency.js"` with a member claim for `a` carrying
`kind: import_specifier`.

## Suspected bugs

Findings from a read-only code review (2026-09-30), each with where to look and
how to confirm. An entry is deleted when it is reproduced and fixed with a test,
or disproved. Status says how far it was checked; "reported" means nobody has
confirmed it.

- **One SCC reads differently by path in the peel kernel.** Status: reported,
  likely benign. The post-seed reporting in `peel/quotient.rs` keeps an SCC
  whose owners collapse into one class, while the `translate_*` helpers drop it
  and `would_be_cycles_after_contract` fabricates a two-class evidence.

## CLI usability

Open usability and scripting-safety findings from exercising the documented
workflows against a real spec; resolved items are deleted. Corpus-specific
paths and source-unit IDs belong in the consuming repo.

### Workflow and iteration follow-ups

- **W1 — Execute commands from a verified snapshot context.** Reuse the current
  snapshot manifest to supply the engine, graph, matching spec, selector lookup
  root, physical input root and package relocations for queries and edits. Avoid
  rebuilding those flags in each worker. Record argv and cwd before dispatch,
  then stdout, stderr and terminal status, including setup failures. Preserve
  provenance refusals and explicit regeneration; add no legacy manifest support.
  A relocated synthetic snapshot should support a normal query through this
  entry point, and stale inputs must still fail.
- **W2 — Make cheap preflight the routine path.** Resolve selectors with their
  complete chunk context, including joint anchors and non-owning names, before
  a whole-spec assignment replay. Reuse the source-only validation mode where
  it supplies that context; close any missing tree/global-context support.
  Include a deleted-anchor example caught before full analysis. Use bounded
  `describe` output first; the concise explain mode below covers missing compact
  diagnostics.
- **W4 — Avoid unrelated rebuilds and unnecessary full graphs.** Investigate
  chunk-scoped selector/projection reuse and runtime-fixture invalidation so an
  ownership edit does not regenerate independent whole-program fixtures.
  Preserve explicit cross-chunk dependencies and source/spec/engine identities.
  Use ordinary reports for assignment and acceptance probes; request complete
  graph sidecars when the operation needs them. Check the combined canonical
  result and relevant runtime behavior after reuse. Coordinate the implementation
  with the existing incremental/codegen work in <perf/proposer.md>.
- **W5 — Reduce cached-emission and graph-export memory.** Analysis reuse still
  leaves large action peaks in these later stages. Identify duplicate retained
  representations and unnecessary lifetimes, then remove a demonstrated source
  of retention. Use matching-input RBE per-action peaks to check the change;
  use a real profiler when needed to choose the implementation. Preserve facts,
  ordering, emitted behavior and report completeness. Improve the existing
  stages without adding a parallel artifact format or compatibility path.
- **W6 — Explain reciprocal cycle paths in ownership-subset reports.** Subset
  runs reject full graph export; their ordinary cycle reports can identify an
  actual SCC and one cut without exposing the return path. This leaves an
  author unable to inspect the entire dependency behind a proposed co-location
  or ordering annotation. Emit a bounded closed-walk witness from the actual
  constraining graph, with source-unit identities, edge causes and residual
  endpoint names. Preserve truncation counts and distinguish actual cycles
  from hypothetical collapsed assignments. Verify that every reported step is
  an active edge for that exact subset; do not require a complete graph dump.
- **W7 — Query integrity evidence without exporting complete graphs.**
  `explain-integrity` currently requires a dependency graph and its program
  graph. A cached-analysis ownership round may have neither, even when the
  saved analysis already contains the native mutation census needed to audit
  specific property slots. Expose that existing typed evidence through a
  source-matched native inspection path. Preserve engine/source/projection
  guards and the distinction between a known exact-slot mutation and a
  conservative unknown receiver. An older audit or an intact unrelated
  intrinsic family must not silently certify the queried slot.

- **W8 — Keep coarse residual routing useful after initializer-layer splits.**
  A real authoring round removed named-owner cycles by separating early helper
  caches from late layout caches, but coarse routing still produced a
  residual-only ESM TDZ cycle; the same claims passed with per-atomic routing.
  The passing fallback emitted thousands of tiny residual modules. Minimize the
  coarse-versus-fine difference into a neutral CLI fixture, then improve bucket
  placement or source-order-preserving contraction. Keep export namespace checks,
  the complete assignment gate and emitted ESM validation authoritative. Do not
  treat a failed coarse heuristic as evidence that the authored owner boundary
  is intrinsically impossible or hide it with purity assertions.

### Planner CLI follow-ups

- **Diagnostics toggle for `modules propose`.** `--limit` now bounds
  proposals and diagnostics and the `limits` summary reports totals
  when details are truncated, but there is still no explicit
  diagnostics on/off toggle for first-pass planning, where proposal
  rows are the only thing the caller wants.
- **Concise explain mode.** Proposal/diagnostic structures on
  `describe` are already opt-in (`--include-proposals`), but there is
  still no compact mode focused on: selected source-unit IDentity and source
  span, atomic-group membership, matching proposal (if any), immediate
  constraining neighbors, and the exact reason the owner is not
  landable today.
- **Patch-plan naming.** `coverage` is useful for intersecting existing
  module YAML with atomic-group coverage, but it is not the only way to
  discover readable work: `atoms --readable-only` and `modules propose`
  may show graph-valid work even when no whole patch section is ready.
  Docs and skill text should reserve "plan" for proposed edits that can be
  reviewed/applied, and avoid implying that empty coverage output means there
  is no landable work.
