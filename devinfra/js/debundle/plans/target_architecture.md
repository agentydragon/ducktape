# Target architecture

The shape the debundler converges on. The other plans in this directory are
steps toward it ([§ Plans as steps](#plans-as-steps)). This file burns down
as the delta in [§ From today](#from-today) closes.

## Contract

Debundle moves top-level statements into named modules and proves the move
does not change behavior. It never rewrites statement code. The only
semantic change it can introduce is the permutation of evaluation order that
the ESM loader induces on the emitted modules.

- **Correctness.** Every pair of statements whose relative order the emit
  changes commutes. A conservative analysis shows it, plus author premises
  that are stated explicitly.
- **Fallback.** Co-location is always valid: one module is the original
  program. Analysis precision buys finer modules, never correctness.
- **Scope.** The debundler validates a reordering. It does not prove program
  equivalence in general.
- **Precision.** The analysis is as precise as the real specs need to split
  into reasonable modules, and no more. A rule earns its place by keeping a
  module boundary that a real spec draws; precision no spec uses is not built.
- **Cost.** A full analysis of the largest consumer corpus finishes in under
  4 minutes, ideally 2 (<analysis_performance.md>).

## Stages

### 1. Parse

Each chunk is parsed once. Its top-level statements, with multi-declarator
variables split, are the units (`prepare_chunks.rs`, `js_ast.rs`).

### 2. Analyze

Analysis runs in source order and never reads the assignment. It is cached by
source, analysis projection and engine (`analysis_projection.rs`,
<../docs/bazel_integration.md>).

- **One effect producer.** One domain and one builtin table
  (`analysis_vocabulary/`, `analysis_domain/`, `facts/sync_effects/`) produce a per-statement
  effect set. Its algorithm is owned by <analysis_consolidation.md>: bounded
  interprocedural may-analysis with ordered proof refinement.
  The effects include:
  - reads and writes of cells: bindings including their initialization, heap
    cells, global and intrinsic slots, host queues;
  - invocations;
  - `Havoc`, carrying an explicit reach and cause.

  Purity, binding timelines and heap frontiers are projections of that set,
  not separate analyses.

- **Cross-module summaries** for imported callables, including vendor bodies
  under the swap promise (`cross_module_purity/`, `staged_analysis/program_summary.rs`).
- **Premises are axioms**: `host_behavior`, `program_premises`,
  `chunk_export_purity`, the `chunk_analysis_options` premise lists and member
  annotations. The engine generates an audit list for each premise: the sites
  and facts the premise discharged. Today the list is complete only for
  `reentry_free_operations` (<../docs/premise_provenance.md>), where
  `spec lint` reads it. Each premise also has a runtime probe
  where one is possible. Probes back premises; they never decide a verdict.
- **Refusals for constructs neither target needs** (`chunk_analysis/chunk_admission.rs`: A1,
  A3 and A5, top-level `await`, cross-module rebinding). A refusal is loud and
  names the construct.

### 3. Plan

- **Assignment.** The spec's selectors give the assignment (`spec_resolution.rs`,
  `lowering/materialize/plan_builder.rs`).
- **Loader model.** An exact ESM model computes the emitted evaluation order
  from the emitted import lists (`esm_schedule/esm_import_order.rs`,
  `realizability/esm_simulator.rs`). It is a plain component with no verdict
  of its own, and it runs on the real order even when imports are cyclic.
- **Order search.** The search chooses import orders through phantom side-effect
  imports. It is seeded by hard facts (binding reads, rebinds, definite
  conflicts) and prefers orders near the last accepted one. It never runs
  inside the interpreter.

### 4. Validate

Validation combines translation validation (<translation_validation_gate.md>)
with structural placement obligations.

- **Swapped pairs.** Diff the emitted order against source order. A sweep
  retains ordered reader/writer participants per cell. Full conflict reporting
  traverses conflicting partners in output-sensitive order, merging cells for
  one pair at a time. A pair-specific premise classifies that pair without
  changing the participants or hiding its other conflicts.
- **Diagnostics.** Each conflict is reported with:
  - the statements: X now runs before Y;
  - the cell and the fact that put it in both effect sets;
  - the fix options: co-locate, state a premise, or the construct is refused.
- **Premises.** `commuting_statements` premises apply only to pairs that
  swap and conflict.
- **Separate obligations.** An effect-based relocation verdict must also
  satisfy structural placement and rebinding checks. Vendor stripping can use
  the same effect facts, but retains its replacement, privacy and singleton
  obligations; a reordering check alone cannot establish those properties.

### 5. Emit

- An accepted spec produces a runnable tree.
- A rejected spec produces a readable tree marked unrunnable (`GATE_REJECTED`,
  exit 3, `pipeline.rs`).
- Reports are small by default. Full graphs and evidence are written on
  request.

### 6. Agent tooling

- **Fast loop.** An assignment edit reruns stages 3–5 against cached analysis,
  in seconds (<../docs/bazel_integration.md>).
- **Explain and propose commands.** `explain-*` commands answer why a pair
  conflicts and what a premise discharged. `propose-*` commands suggest
  co-locations, premises and module splits (`cli/`, `peel/`).
- **CI performance budgets** on the canonical inputs
  (<analysis_performance.md>).

## Stage 2 producer

The structure and remaining work are consolidated in the devel-based
<analysis_consolidation.md>. Extend the existing `IterativeInference`, value-flow
rules and program-wide purity/effect closure; do not install another production
solver. The target producer computes bounded, idempotent may-effects through interprocedural
constraints and uses the ordered interpreter for initialization, singleton and
completion proofs. Shared transfers and explicit dependencies replace repeated
pool execution; hypothetical solver visits are separate from actual ordered
observations. Prefix-sensitive escape and availability remain necessary: a
whole-program potential graph cannot make future callbacks available early.

Keep the existing verdict authoritative until the new records, refinement
rules and comparator have their soundness argument and causal CLI evidence.
Changing analysis structure does not authorize an early effect-gate flip or
removing placement, loader, replacement or privacy checks. Unsupported cases
can retain explicit havoc/refusal; precision is justified by real boundaries.

The statement-class prototype's low cost does not establish soundness; its
[independent audit](../debug/summary_effects/2026_11_10_statement_class_independent_audit.md)
contains counterexamples. Preserve those behavioral controls. Do not revive a
prototype solely because it is fast or assume that a generic summary solver
will be near-linear. The new plan requires an input-size bound and completed
same-input measurements before adoption.

## Out of scope

- Proving equivalence of arbitrary code.
- Rewriting statement bodies.
- Runtime differential testing as an oracle. Probes back premises; they never
  decide a verdict.
- Unbounded proof search. When analysis cannot show a pair commutes, the
  outcome is co-locate, a premise, or a refusal.
- Reasoning across versions of an input.

## From today

| Today                                                                | Target                                                                 |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Pass 1 rejects cycles among globally materialized constraining edges | Retired; only swapped pairs are checked                                |
| Pass 2 simulates on a `ModuleId` order when Pass 1 is cyclic         | Exact loader model on the real order, no verdict                       |
| Atomic groups decide acceptance (`atomic_group_conflicts.json`)      | Ordering groups guide planning; structural constraints remain enforced |
| Purity classifier, heap-barrier chain, fallback edges                | Effect sets plus `Havoc` reach                                         |
| Separate relocation and strip checks                                 | Shared effect facts, with placement and strip obligations retained     |
| Premises carry `audited_source_sha256` and evidence in reports       | Premises carry an engine audit list and a runtime probe                |

## Code shape

The implementation converges on these owners inside `facts/sync_effects/`
(details and extraction order: <convergence_landing.md> § Target code shape):
`kernel/` (continuation, completion, narrow context traits), `world/` (semantic
world, object surface, descriptor heap, lexical cells, one join),
`alternatives/` (path-preserving transfer families), `adapters/` (application
and provider execution modes), `native/` (builtin rows through one plan/commit
path), `roots/` (owner execution phases). Each JS operation has one decision
site; each state join has one implementation; refusal is a conservative outcome,
never a panic. `legacy/` holds not-yet-deleted paths behind tombstones and is
empty at the end.

## Plans as steps

- <convergence_landing.md>: landing order on `devel`, debt inventory, exit
  gates for the refactor as a whole.
- <abstract_interpreter.md>: execution-derived purity, remaining certificates,
  complete statement effects, scoped fallback reach and effect-set premise
  checking.
- <synchronous_execution_fallback.md>: `Havoc` reach. It decides how many
  swapped opaque pairs commute.
- <staged_pipeline.md>: when finer-grained S3 or vendor-strip actions justify
  their added artifact and action costs.
- <translation_validation_gate.md>: stages 3 and 4, and the migration from
  Pass 1 and Pass 2.
- `../SPEC.md` § Host behavior and § Reentry through imported names: explicit
  host premises and their effect on export reentry.
- <analysis_consolidation.md>: stage 2 analysis algorithm, bounds and migration.
- <analysis_performance.md>: stage 2 cost and the CI budgets.
- <native_receiver_identity.md> and <../TODO.md#purity-classifier>: stage 2
  analysis precision.
- <relational_selectors.md>, <automated_spec_workflows.md>,
  <explicit_import_order.md>: the spec surface and stage 6.

## Known open problems

Historical evidence: <../debug/tana_scc/README.md> and
<../debug/tana_scc/2026_10_31_pure_call_heap_frontier.md>. Current gate outcomes require
replay of the canonical inputs in gaffer-private.

- **Heap locality for vendor callees.** Opaque write-capable callees retain
  conservative heap barriers. Finer modules need an audited locality premise
  or a derived vendor summary, plus coverage of incomplete execution. A
  callback-reinvocation promise alone supplies no heap-locality proof.
- **Fallback reach.** Swapped statements with incomplete execution conflict
  under global reach. Bounded reach remains report-only until the complete
  effect-domain proof is retained.
- **Unknown without escape.** Value sources lowered to "unknown" (a closure's
  write to an outer local, array-destructuring declarations, `for`-`of` heads,
  generator results, `arguments`, `catch` parameters, iteration-row results)
  did not escape the callables they can hold, so a later call through the
  unknown value missed their effects. Fixes apply one rule at every such
  site: a value is dropped from a tracked set only after it has been escaped.
  The probes are the regression suite; see
  <convergence_landing.md> § Replan for the landing order. Still open after
  the iterator-site and receiver series:
  - `arguments`, `new.target` and a `super` home object read by a truncated
    remainder (no accepted-divergent shape found yet); `super` does not mark
    a method as reading `this`.
  - Prototype chains built by `Object.create` or `Object.setPrototypeOf`; only
    a literal `__proto__` chain is followed for accessors.
  - The kernel lane's own `for`-`of` and destructuring over non-native values
    are unaudited.
  - Precision gaps, all refused: `({ ...gen() })`; an `Object.assign` copy of
    an uncalled effect, including a config merge
    `Object.assign({}, defaults, { a: 1 })`; a `this`-reading method or
    accessor on an object holding an uncalled effect (`api.size()`), whose
    receiver is published.
- **Callback-effect provenance.** The unknown global-property dispatch defect
  is fixed; the separate dense-array callback-promotion route is still an
  unproven candidate. Canonical attribution needs per-call target/contributor
  evidence that the batch report does not retain. See
  <../docs/lessons_learned/effect_coverage_and_calls.md> § Calls; the old batch
  counts do not establish membership in current residual components.
