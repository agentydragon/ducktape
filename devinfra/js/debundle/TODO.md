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
- <plans/explicit_import_order.md> — explicit-order delivery contract, residual
  proof and the remaining downstream and selected-view work in W9 below.
- <plans/native_receiver_identity.md> — native receiver identity and its
  descriptor, alias, method-receiver and initialization proof obligations.
- <plans/convergence_landing.md> — gate-migration debt, discrepancy controls
  and exit gates; draft #14552 is optional reference material.
- <plans/analysis_consolidation.md> — devel-based keep/extend/delete decisions
  for existing analyses; shared dataflow/effects, bounded cross-chunk propagation,
  ordered proof refinement and polynomial cost obligations. No dependency on #16762.
- <plans/analysis_performance.md> — open impact-profile, callback, provenance,
  relation/dependency and preparation-reuse cost tasks and completion evidence,
  plus PS1, PS2 and PS4 pass contracts and
  shared infrastructure;
  algorithm design stays in the consolidation plan.
- <plans/abstract_interpreter.md> — migrate the bespoke semantic certificates
  onto one abstract interpreter, domain and builtin summary table.
- <plans/synchronous_execution_fallback.md> — make modeled effects authoritative
  for incomplete at-init calls before removing the graph builder's conservative
  read-closure fallback; reported havoc reach is not yet consumed.
- `perf/` — measured performance notes. Update from real
  profiles before major matcher/index rewrites.

Planning hygiene: keep active dispatch order here. When a plan's core work is
complete, summarize only its remaining tail here instead of leaving the plan as
a second priority queue.

### Remaining dispatch order

Safety evidence outranks speculative deletion counts. Source inspection is not
an end-to-end reproduction; performance candidates require fresh opt profiles.
The sections below hold details, not competing priority queues.

1. **Soundness regressions.** Preserve behavior and conservative fallback
   throughout the work below. Use the cyclic export-cell item and AR7 for
   concrete soundness gaps, with causal CLI evidence and canonical controls.
2. **Shared analysis and bounded propagation.** Follow
   <plans/analysis_consolidation.md> and the [performance tasks](plans/analysis_performance.md#open-analysis-tasks).
   Restore bounded private-consumer completion (PF0), including the Excalidraw
   10% regression bound. Finish namespace provenance (PF6) and address measured
   Tana costs through source transfers and dependency registration together
   (PF7–PF8). Take bounded preparation reuse (PF9) independently. Rank remaining
   loader, receiver-flow and containment changes (PF10–PF12) by measured corpus
   costs. Record matched impact profiles (PF1, PF4), replace callback replay as
   dependencies become explicit (PF5), and standardize pass contracts alongside
   those migrations (PS1, PS2, PS4). Independent sealed graph passes can proceed
   before general notification infrastructure.
3. **Consumer consolidation and gate migration.** AR1–AR4 follow the shared
   facts and operation semantics above. Delete each superseded decision path
   with its last consumer; name any remaining legacy authority and removal
   gate. Additional coverage and the effect-gate flip retain the independent
   obligations in <plans/convergence_landing.md>. Neither draft #14552 nor
   #16762 is a prerequisite.
4. **Single-input spec assistance.** Keep selector synthesis and repair
   diagnostics scoped to one configured input program and one exact spec.
   Prioritize correct edits and explicit skipped-candidate reasons. Proposed
   selector edits are proved by the shared structural resolver; diagnostics
   remain suggestions, and the author decides what to apply. Keep
   gate-before-write, atomic writes and dry-run reporting.
5. **Excalidraw smoke coverage.** Exercise post-startup locale, font-subsetting
   and file-system imports. Capture a representative editor state with
   `VisualPage.capture()` for PR review. See the [public corpus and current
   browser checks](excalidraw/README.md).
6. **Profile-backed AST and lookup work.** Measure cloning, file-name lookup
   and alpha-binding snapshots before ownership redesign, indexes or undo logs.
   Preserve ordering/hygiene and the independent Node differential oracle.
   Anonymous uniqueness uses coarse semantic-token buckets with exact equality;
   profile real workloads before adding indexes or other machinery.
7. **Remaining selector authoring gaps.** Follow the selector diagnostics above
   and minimizer work below. Keep near-miss ranking heuristics distinct from
   boolean backtracking.
8. **Remaining responsibility seams.** Use AR4–AR5 and AR8 for bounded ownership
   and identity changes that simplify real callers. AR6 needs a lowering-design
   decision before implementation. Prefer deleting redundant setup over deleting
   behavioral assertions or adding test frameworks.

### Cyclic export-cell observation soundness

- Cover future export-cell observations by arbitrary unknown execution.
  The source-derived may-read path follows source-known calls and local
  re-export aliases. Namespace calls and unresolved host/global callees remain open. The broad fence
  tried during merged [PR #16539](https://github.com/agentydragon/ducktape/pull/16539)
  rejected the frozen Excalidraw spec (868-unit atomic group).
  <debug/2026_11_14_cyclic_export_cells.md> records the reproduction,
  exact checks, policy blockers and witness. The remaining global-call case
  is accepted by the ordinary gate on `devel` at `fc6336654b` without author
  premises; see <docs/design.md#soundness-boundary-and-regressions>. Next lane:
  establish loader reachability or transport the missing cell-access
  obligations without weakening the gate. Close when the opaque observer counterexample is
  rejected and the unchanged canonical reconstruction passes.

### Architecture review follow-ups

**Split selector resolution from placement checking.** Both the Rust and Lean
workflows need Rust's [selector resolver](docs/selector_resolution.md).
Starting from `spec resolve-placement`, make it a standalone binary: given
the pinned source and the authored selectors, produce the requested plan
(source digest, resolved unit identities, destination modules and authored
import priority). It should report missing or ambiguous selectors on its own.
A successful resolution says where the author wants the units to go.

Give the Rust gate a separate binary. It and the [Lean splitter](x/split_proof/PLAN.md)
must consume the same versioned placement interface. Write one resolved plan
to disk and pass that very file to each binary with the pinned source and its
checker-specific assumptions. Keep selectors and checker verdicts out of this
interface. Lean must check placement, observable behavior and its own
premises against the actual output. Neither checker may silently repair the
requested assignment or inherit the other checker's verdict. Separate the
authored placement fields from the gate-specific assumptions in the spec
schema first. Consider separate files or directories once both consumers
use the interface; keep existing specs usable during migration.

Add a compatibility test that resolves once and passes the artifact unchanged
to both binaries, including a stale source digest that both must reject.
Run both checkers on identical plans for real, version-pinned programs. Record
what each certifies, refuses or cannot model, the assumptions it used, and
the output of independent behavioral checks. Keep known Rust false positives
as regression cases. Once Lean certifies the placements and guarantees needed
by a production workflow, including rejecting bad output on disk, switch that
workflow to Lean. The two workflows may also share materialization: one
writer assembles the module tree from the resolved plan. Lean must still
reparse the actual emitted files and certify that tree against the source
and plan before publishing it. A printer mutation that changes an exported
binding should fail that check even if both workflows used the same writer.
The goal is one proven-correct splitter, with Lean as the sole certification
authority. Retire the Rust gate when its last consumer has migrated; the
shared Rust resolver remains useful.

For the first multi-chunk placement format, each emitted module draws its
source units from one input chunk. A chunk may split into many modules. Keep
each original entry and lazy loading boundary, and check the resolved edges
and initialization order of the whole emitted graph, including generated
glue. An eager entry must not start evaluating a plugin just because the two
chunks use the same helper.

Explore reusable Lean proofs at static ESM boundaries. A component summary
should describe a transition from related incoming states, with corresponding
imported live cells, to corresponding effects, outcomes and exported live
cells under the declared host contract. Importers can call exported code
later. Equal namespace values just after initialization are insufficient.
Compose summaries in dependency order for acyclic imports, preserving shared
module identity and evaluate-once behavior. Treat a cycle as one proof unit
so reads of uninitialized bindings and callbacks across the cycle remain
visible. Keep the whole-graph check as the reference while comparing proof
cost and reuse; dynamic loading and async evaluation need their own loader
and scheduling contract.

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
  Complete read-only lexical-observer pools use shared source certificates
  and current-prefix primitive-return checks without replay
  (<docs/inference.md>). Remaining families include writers, source calls,
  foreign callbacks, captured environments and heap operations.
  `invoke_callback_pool_once` in <facts/sync_effects/interpreter/callback_promotion.rs> supplies
  other callbacks as possible arguments, evaluates against evolving state, and
  falls back at depth, expansion and pool limits. Reduce repeated cross-product
  exploration with proved callee/reach summaries and shared effect composition;
  keep global/unknown behavior explicit. Also distinguish a callback's
  primitive argument from its collection receiver
  (`e2e/callbacks/iteration_receiver.rs`): the receiver-preservation proof, its
  consumers and acceptance criteria are in
  <plans/analysis_consolidation.md#next-bounded-callback-slice> and
  <debug/2026_11_17_receiver_confinement.md>. Capability and effect-authority
  work also follows <plans/abstract_interpreter.md> and
  <plans/synchronous_execution_fallback.md>. Close with that slice's positive
  CLI case and adversarial negatives (receiver escape or writes, changed
  descriptors or prototype, replaced callback-producing methods, getters or
  proxies, recursion, dynamic scope,
  truncated execution), each keeping conservative behavior. Also retain
  controls around the current limits, callback order and reentry, plus a real
  profiler comparison on identical inputs. Exhaustion must retain conservative effects, dependencies
  and precision-loss evidence. Increasing limits alone is not a resolution; the
  rejected global-prefix fixpoint in <docs/design.md> needs new evidence before
  reconsideration.

- **AR3 — Compose callable capabilities and initialization availability.**
  `CallableSummary` in <analysis_vocabulary/inputs.rs> and <cross_module_purity.rs> still
  distribute return, callback, capture and availability proofs across
  specialized paths. Source initialization sites and summary eligibility share
  the flow census (<docs/inference.md>); reached-prefix readiness remains
  separate ordered evidence.
  Closed passive providers admit multiple inert declarations; providers with
  imports or active initialization still lack a reached-prefix readiness proof.
  Reuse the shared interpreter to compose these independent facts through
  canonical import, alias, namespace and reexport identities; prove availability
  under emitted initialization order before broadening provider shapes. Close
  with local/imported equivalent-shape controls, initialized providers beyond
  closed passive groups, and mutation, capture and early-cycle negatives, preserving
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
  semantic records still owned by `analysis_vocabulary/report_schema.rs` to domain owners, retaining
  <analysis_vocabulary/execution_evidence.rs> and report projections. Close when real callers use
  those interfaces without reverse ownership, with unchanged semantic facts,
  report/saved fields, proof provenance, gate results and staged replay. File
  moves alone do not establish that boundary. PS1/PS2 in
  <plans/analysis_performance.md#pass-standardization-tasks> track the concrete
  pass-contract and sealed-input handoffs.

- **AR5 — Reuse one prepared source/index across analysis phases.**
  <frontend_elaboration/elaboration.rs> and <staged_analysis/chunk_facts.rs>
  recollect structural facts. Prepared syntax now survives through the selected
  static provider closure; consolidate the remaining censuses. Extend
  `SourceProgram`/manifest ownership so selectors, provider analysis and local
  facts reuse immutable syntax and indexes across the required provider closure.
  Close when those production paths no longer build duplicate structural censuses, while exact source sites, repeated declarations,
  hygiene, provenance and staged replay remain stable. Preserve `SourceWithAst`
  mutation invalidation, its release at the `EmissionFiles` boundary, and actual
  source-marker/pass-through-byte controls. Profile retention and memory before
  claiming a performance improvement. Use PS1/PS2 to make producer ownership,
  shared completed inputs and retirement explicit at each migrated boundary.

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

- **AR7 — Finish the conditional soundness contract audit.**
  <docs/design.md#soundness-boundary-and-regressions> now separates false author
  premises from independent engine defects. The former can change behavior;
  <e2e/commuting_statements_gate_test.rs> demonstrates an intentionally false
  commutation promise and a modeled contradiction the gate rejects. The latter
  occurred for foreign mutable-state reads without a premise; see
  <debug/2026_11_09_l4c_discrepancy_audit.md#unsafe-legacy-acceptance>, its
  repaired-gate fixture <e2e/foreign_cell_read_order_test.rs>, and the
  accepted-output sweep <e2e/foreign_state_node_differential_sweep_test.rs>.
  The current cyclic export-cell defect is separately tracked under
  [Cyclic export-cell observation soundness](#cyclic-export-cell-observation-soundness).
  Check remaining host and dynamic-scope assumptions against neutral CLI cases
  and A1's admission rules. Record any newly reproduced ordinary-gate unsafe
  acceptance with a minimal bundle/spec and repair it conservatively; do not
  infer one from stale prose, `--emit-on-rejection`, or a report-only prototype.
  Close when the documented preconditions and executable cases agree. Finite
  fixtures and expected rarity do not prove universal soundness.

- **AR8 — Separate qualified module identity from author spelling and output paths.**
  `ModulePath::parse` in <spec/spec.rs> folds case and accepts chunk-qualified
  spellings; `logical_module_path` in <selectors/resolution/selector_resolve/>
  and <lowering/materialize/plan_builder.rs> independently split qualified
  strings. Use a typed chunk-qualified logical reference between services and
  convert to author/wire spelling or physical output paths only at boundaries.
  Decide and document whether case folding is an authoring rule before changing
  accepted input. Close with all callers updated together and CLI/report controls
  for same-named modules in different chunks, case collisions, normalized paths
  and supported qualified spellings. Preserve Linux path semantics without
  introducing transitional identity shims.

- **AR9 — Group configuration by chunk.** Consider replacing parallel
  `<chunk>.<setting>` maps with one typed chunk record, optionally in per-chunk
  YAML files. Keep this separate from the explicit-order delivery: preserve
  chunk identity, source/spec provenance and semantic analysis cache keys.

- **AR10 — Reconcile generated atomic-unit modules with explicit import order.**
  Requiring authors to enumerate generated owner names and their priority makes
  `per_atomic_group` routing less automatic. Decide whether module generation
  should become an explicit, reviewable authoring step that persists owners and
  an order witness together, or provide another workflow with clear guarantees.
  Cover owner-name stability and changes to the generated owner set; do not hide
  arbitrary scheduling search or silently repair an invalid order. Close with
  documented CLI behavior and tests for first generation and later spec edits.

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

1. **Runtime resource controls.** `e2e/emission/url_rebase.rs` covers lazy JavaScript and
   stylesheet reads through `new URL(..., import.meta.url)` under Node. Still
   uncontrolled: URL search/fragment and encoded path identity, nested CSS asset
   references, real browser worker/style loading, dynamic consumers of package
   replacements, and the direct `Worker`-string rewrite (whose browser document
   base is not the module URL; the resource inventory marks it unresolved).
2. **Ground selector-stabilization skill fixtures.** Add tested, anonymized
   fixtures for the common anchor-choice cases of the `debundle_stabilize`
   playbook so its guidance is executable rather than only prose.
3. **H1 — Combine source-marker policies with retained constructors.** Optional
   integration coverage: select a neutral provider through `source_markers`,
   apply `retained_callback_constructor_exports`, construct an object that stores
   a callback, then invoke it after its captured lexical value initializes.
   Assert fixed original/emitted callback values and constructor-effect traces,
   intended module ownership and provider context. Removing the retention
   contract should reject a split that depends on it. Start from
   <e2e/source_export_policy_test.rs> and
   <e2e/retained_constructor_callback_test.rs>; the two features have independent
   coverage already.
4. **H3 — Scope additional intrinsic-family probes.** Optional investigation,
   not a confirmed coverage gap. Before adding tests, identify a concrete
   `program_premises.intrinsics_never_written` premise used by a passing private
   spec, a distinct supported operation, and an observable result or rejection
   that existing tests do not establish. Start from
   <e2e/intrinsics_never_written_test.rs>.

### Encountered tooling friction

- **F5 — Decide the remaining premise kinds from provenance.**
  <docs/premise_provenance.md> lists where each premise is consumed, whether
  every read records its use, and what the default `spec lint` decides. Paused
  behind the architecture convergence; resume one kind per change, outside
  `facts/sync_effects/`, linting each the same way (never consumed →
  `inferable`, consumed → probe):
  - a per-statement use list written at each classifier arm for read
    `purity: pure` hints and `pure_new` / `pure_members` (never a
    `ProofDependencies` field); unread `purity: pure` hints are already linted;
  - `predefined_globals`: three blockers and a structural alternative in
    <docs/premise_provenance.md#predefined_globals>;
  - per-chunk or parallel `--probe-analysis` runs, to bound probe cost on
    large inputs;
  - the hot-path kinds (`reentry_free_builtin_modules` / `_bun_modules` static
    closure, `initializer_call_effect_inputs`, `deferred_global_publications`,
    `initializer_purity`, `no_reentry_*`), probe-only until the
    `callback_values` performance work lands.
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

- **F7 — Explain callback-fallback read provenance.** A pool-limit report names
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

- **Vendor swaps on a production esbuild corpus (Excalidraw).** Observed while
  authoring <excalidraw/spec/vendor_marks.yaml>:
  - A `partial_swap` package whose subpath is JSON (`open-color.json`) fails
    `DebundleProjectAnalysis` with the bare parser error
    `failed to parse <path>: ExpectedSemiForExprStmt`, naming neither the mark
    nor the package. Reject non-JS subpaths at spec validation with the mark's
    identity, or support JSON defaults as `swap` does.
  - A `partial_swap` package has one subpath, so a chunk exporting values from
    several entry files of one package (roughjs `bin/rough.js`,
    `bin/generator.js`, `bin/math.js`) cannot be swapped. Next action: decide
    whether a symbol may name its own subpath.
  - `chunk_renames` selectors accept only `binding` names, so renaming a
    residual binding requires a minified-name pin; the corpus renames through
    module members instead.
  - `(e => e)({...})`, an immediately invoked local identity arrow, is an
    `unknown_call` initializer, which put `APP_STATE_STORAGE_CONF` on the side-effect chain.

### Pipeline performance and architecture cleanup

Open analysis cost tasks and their completion criteria live in
[the analysis performance tracker](plans/analysis_performance.md#open-analysis-tasks).
Their priority stays in the dispatch order above.

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

Placement-gate, `debundle run` (report opt-out, chunk-level incremental
rebuilds, codegen cache), and materialize-stage performance work lives in
<perf/pipeline.md>.

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

- **Statement-level purity override.** Member `purity: pure` covers calls;
  `initializer_purity: pure` covers a simple variable initializer. Consider a
  statement-level override for an otherwise unprovable source unit outside those
  contracts. Prefer compositional analysis for recurring expression families.
- **Redundant-hint guardrail.** Use [spec lint](SPEC.md#spec-lint)
  to check for newly inferable purity hints in CI or pre-commit, with a narrow
  allowlist for intentional hints. Validate each removal against the resulting spec.

The ignored `inferred_pure_collection_constructors_with_literal_args_emit_no_s_cycle`
fixture is a deferred **RegExp admission feature**, not a contradictory classifier
bug. The `RegExp` row in `facts/abstract_domain/builtins.rs` throws on pattern
values, so purity needs static ECMA-262 pattern validation before admitting
literal `RegExp` construction; the active classifier test correctly expects
Unknown today. Keep the future test ignored until that precondition is
implemented; do not admit constructors that can throw.

Infer literal-string `TextEncoder.encode` initialization from a proved fresh
receiver and intact constructor/method identities. The accepted split in
`e2e/initializer_source_only_test.rs` currently needs explicit initializer purity
and lexical-effect promises. Its dataflow-aware allocation fixture additionally
uses either commuting promises against independent allocations or the scoped
`initializer_external_effects: isolated` author contract. Purity and lexical effects
alone do not establish heap independence, and commuting with one barrier preserves
ordering through earlier statements. Compose precise heap/call effects
and native proof dependencies so those promises can be removed; retain fallback
for shadowing, replacement, accessors and coercing arguments. Follow
<plans/native_receiver_identity.md> and verify a real extraction gain before
changing gate behavior.

Infer result-owned private metadata effects where source execution can prove
fresh runtime key ownership, non-publication, stable provider/configuration
observations and observationally idempotent private memoization. The scoped
`initializer_external_effects: private_metadata` author promise covers these
obligations today. Keep known heap/global conflicts and provenance; do not infer
this contract from a WeakMap or method name. Require bundle/spec runtime E2E
coverage and a demonstrated downstream extraction gain before adding inference.

Heap-isolation admission currently rejects any source-observed content access
without a fresh runtime ownership proof. Do not whitelist a cell merely because
its allocation site occurs in the same initializer's invoked helpers: source
origins conflate earlier, recursive and deferred runtime generations. Extend
admission only with a real source/spec acceptance case requiring stronger fresh
ownership evidence.

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

## CLI usability

Open usability and scripting-safety findings from exercising the documented
workflows against a real spec; resolved items are deleted. Corpus-specific
paths and source-unit IDs belong in the consuming repo.

- **Align engine executable names.** The pipeline currently invokes `debundle`,
  `debundle_analyze` and `debundle_project_analysis`, while the benchmark calls
  them debundler, analyzer and projector. Choose consistent executable names
  (for example, those three role nouns) or consolidate the stages into one CLI.
  Update Bazel labels, callers, environment variables and documentation together.

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
  `describe` output first.
- **W4 — Avoid unrelated rebuilds and unnecessary full graphs.** Investigate
  chunk-scoped selector/projection reuse and runtime-fixture invalidation so an
  ownership edit does not regenerate independent whole-program fixtures.
  Preserve explicit cross-chunk dependencies and source/spec/engine identities.
  Use ordinary reports for assignment and acceptance probes; request complete
  graph sidecars when the operation needs them. Check the combined canonical
  result and relevant runtime behavior after reuse. Coordinate the implementation
  with the existing incremental/codegen work in <perf/pipeline.md>.
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

- **W9 — Complete downstream explicit-order adoption and extraction acceptance.**
  Follow <plans/explicit_import_order.md>. Bound the source-analysis fact-store
  memory before resuming large consumer runs. Migrate and validate
  every configured downstream consumer with its reviewed complete priority;
  preserve diagnostic and passing controls as distinct inputs. Update maintained
  downstream guidance and land the production pin only after the final engine
  passes fresh configured validation and emitted-runtime checks. Demonstrate
  useful unseeded discovery, check, preview, application and behavior on that pin.
  Selected-view semantic premises and selector anchors remain grounded in the
  full authored tree while the selected placement view is edited; do not
  substitute a rejected canonical graph or weaken full-context constraints.

- **W10 — Prove additional useful residual families.** The first complete
  constructor only moves true-entry atoms to one fresh owner while retaining the
  old traversal. Existing-owner extensions, generated/catchall residual owners,
  reordered traversals and beneficial owner reorganization need separate
  algorithms and proofs. Report unsupported routes explicitly. Extend the
  supported scope only with a useful residual-reduction example, independent
  small-case soundness/completeness evidence and prepared-query measurements;
  do not silently widen the initial family's negative result.

### Cache graph export across authored presentation edits

Default apply can reuse its canonical typed projection after YAML presentation
or module-level note edits. Full graph export still captures all authored bytes
and YAML membership before lowering, and report writers enrich evidence with
authored notes and locations. A shared core for graph export needs typed deferred
provenance/report finalization: verify the current authored projection against the
core, retain source/vendor/native compatibility evidence, then bind the exact
current inventory. Require accepted and rejected graph replay tests, note and
membership freshness, relocation between execroots, and measured Bazel cache
reuse before removing raw YAML from graph export inputs.

## Deferred architecture decisions

- **Serialization for generated spec types.** Check whether protobuf-aware
  serialization can replace `spec_wire_serde_enums` in `BUILD.bazel` while keeping
  authored YAML and JSON field presence, unknown-field rejection and snake_case
  names. Test schema-derived diagnostic spellings for `ReentryFreeOperation` and
  `InitializerExternalEffects` before removing their Strum `Display` derives.
- **Whole-chunk intrinsic integrity.** The census guards exact slots for migrated
  consumers. Hidden writes and host mutations remain the author's
  [A11 obligation](docs/design.md#conditions-on-the-input-chunk-assumptions-a1a11).
  Measure the cost and reach of a mandatory whole-chunk check before changing
  that contract. Purity consumers follow <plans/abstract_interpreter.md>.

## Retained spec capabilities

The 2026-11-14 census of gaffer-private authored specs, Tana canonical,
baselines and fixtures found no configured uses of the audited opt-ins. Public `partial_swap` and `no_reentry_callback_exports` callers are in
<excalidraw/spec/vendor_marks.yaml> and
[the persistent registry example](skills/shared/examples/contract_examples/persistent_registry/spec_config.yaml).
[The audit retained each capability](https://github.com/agentydragon/ducktape/pull/16739/files)
for a distinct contract or caller. Recheck current consumers and identify the
contract change or equivalent replacement before proposing a retirement.
