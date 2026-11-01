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

## Stages

### 1. Parse

Each chunk is parsed once. Its top-level statements, with multi-declarator
variables split, are the units (`prepare_chunks.rs`, `js_ast.rs`).

### 2. Analyze

Analysis runs in source order and never reads the assignment. It is cached by
source, analysis projection and engine (`lowering/analysis_projection.rs`,
<staged_pipeline.md>).

- **One abstract interpreter.** One domain and one builtin table
  (`facts/abstract_domain/`, `facts/sync_effects/`) produce a per-statement
  effect set:
  - reads and writes of cells: bindings including their initialization, heap
    cells, global and intrinsic slots, host queues;
  - invocations;
  - `Havoc`, carrying an explicit reach and cause.

  Purity, binding timelines and heap frontiers are projections of that set,
  not separate analyses.

- **Cross-module summaries** for imported callables, including vendor bodies
  under the swap promise (`cross_module_purity.rs`, `lowering/cross_module.rs`).
- **Premises are axioms**: `host_behavior`, `program_premises`,
  `chunk_export_purity`, the `chunk_analysis_options` premise lists and member
  annotations. The engine generates an audit list for each premise: the sites
  and facts the premise discharged. Each premise also has a runtime probe
  where one is possible. Probes back premises; they never decide a verdict.
- **Refusals for constructs neither target needs** (`chunk_analysis/chunk_admission.rs`: A1,
  A3 and A5, top-level `await`, cross-module rebinding). A refusal is loud and
  names the construct.

### 3. Plan

- **Assignment.** The spec's selectors give the assignment (`spec_resolution.rs`,
  `lowering/materialize/plan_builder.rs`).
- **Loader model.** An exact ESM model computes the emitted evaluation order
  from the emitted import lists (`esm_import_order.rs`,
  `realizability/esm_simulator.rs`). It is a plain component with no verdict
  of its own, and it runs on the real order even when imports are cyclic.
- **Order search.** The search chooses import orders through phantom side-effect
  imports. It is seeded by hard facts (binding reads, rebinds, definite
  conflicts) and prefers orders near the last accepted one. It never runs
  inside the interpreter.

### 4. Validate

Validation is translation validation (<translation_validation_gate.md>).

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
- **One conflict check.** Validation has no pass-level components. TDZ, the
  commuting-premise check and the vendor strip's split-brain and
  observable-effect checks (`vendor/strip.rs`) are all this check, applied to
  the reordering each one performs.

### 5. Emit

- An accepted spec produces a runnable tree.
- A rejected spec produces a readable tree marked unrunnable (`GATE_REJECTED`,
  exit 3, `pipeline.rs`).
- Reports are small by default. Full graphs and evidence are written on
  request.

### 6. Agent tooling

- **Fast loop.** An assignment edit reruns stages 3–5 against cached analysis,
  in seconds (<staged_pipeline.md>).
- **Explain and propose commands.** `explain-*` commands answer why a pair
  conflicts and what a premise discharged. `propose-*` commands suggest
  co-locations, premises and module splits (`cli/`, `peel/`).
- **CI performance budgets** on the canonical inputs
  (<analysis_performance.md>).

## Out of scope

- Proving equivalence of arbitrary code.
- Rewriting statement bodies.
- Runtime differential testing as an oracle. Probes back premises; they never
  decide a verdict.
- Unbounded proof search. When analysis cannot show a pair commutes, the
  outcome is co-locate, a premise, or a refusal.
- Reasoning across versions of an input.

## From today

| Today                                                                                | Target                                                  |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| Pass 1 rejects any cycle among 2.1 M globally materialized constraining edges (Tana) | Retired; only swapped pairs are checked                 |
| Pass 2 simulates on a `ModuleId` order when Pass 1 is cyclic                         | Exact loader model on the real order, no verdict        |
| Atomic groups decide acceptance (`atomic_group_conflicts.json`)                      | At most a planning aid                                  |
| Purity classifier, heap-barrier chain, fallback edges                                | Effect sets plus `Havoc` reach                          |
| Separate checks: strip split-brain/privacy, commuting gate, TDZ                      | One conflict check                                      |
| Premises carry `audited_source_sha256` and evidence in reports                       | Premises carry an engine audit list and a runtime probe |
| Analysis recomputed on every edit                                                    | Cached; assignment edits rerun stages 3–5               |
| `dependency_graph.json.gz` (about 1 GB before compression)                           | Small default reports                                   |

## Plans as steps

- <abstract_interpreter.md>, steps 4c–9: the single interpreter and its effect
  sets. Step 7 supplies the cells validation reads. Step 9 (`EffectsDisjoint`)
  is the pair check.
- <synchronous_execution_fallback.md>: `Havoc` reach. It decides how many
  swapped opaque pairs commute.
- <staged_pipeline.md>: the stage 2 cache and the fast loop.
- <translation_validation_gate.md>: stages 3 and 4, and the migration from
  Pass 1 and Pass 2.
- <host_behavior.md>: host premises as explicit behavior keys.
- <analysis_performance.md>: stage 2 cost and the CI budgets.
- <native_receiver_identity.md>, <purity_analysis.md>,
  <runtime_validation.md>: analysis precision and probe work within stage 2.
- <relational_selectors.md>, <automated_spec_workflows.md>,
  <module_proposals.md>: the spec surface and stage 6.

## Known open problems

Evidence: `debug/2026_10_31_tana_scc_pessimism.md`,
`debug/2026_10_31_pure_call_heap_frontier.md`, and the swapped-pair
measurements in <translation_validation_gate.md>.

- **Heap locality for vendor callees.** Tana's 1,335 heap-barrier statements
  form one total chain. Foreign-held arrays exist from statement 0, and only 4
  statements observe arrays precisely, so no small sound change breaks the
  chain. Every gate, including translation validation, needs heap-locality
  knowledge for the write-capable callees: an audited premise or a vendor
  summary, plus completed execution of the incomplete statements.
- **Fallback reach.** Under a realizable order, 6,079 swapped pairs on Tana
  are between two statements with incomplete execution. With a global reach
  they all conflict.
- **The "observed" provenance label covers pool-callback effects.** 21,812 of
  Tana's 23,102 observed `binding_timeline` edges come from statements that
  hit the escaped-callback pool limit. "Observed" must mean an observed effect
  of the statement itself, or diagnostics and premise contradiction checks
  trust effects that only the pool produced.
