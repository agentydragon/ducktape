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
source, analysis projection and engine (`staged_analysis/analysis_projection.rs`,
<../docs/bazel_integration.md>).

- **One effect producer.** One domain and one builtin table
  (`facts/abstract_domain/`, `facts/sync_effects/`) produce a per-statement
  effect set. How it is computed is an open decision
  ([§ Open decision](#open-decision-the-stage-2-producer)):
  - reads and writes of cells: bindings including their initialization, heap
    cells, global and intrinsic slots, host queues;
  - invocations;
  - `Havoc`, carrying an explicit reach and cause.

  Purity, binding timelines and heap frontiers are projections of that set,
  not separate analyses.

- **Cross-module summaries** for imported callables, including vendor bodies
  under the swap promise (`cross_module_purity.rs`, `staged_analysis/program_summary.rs`).
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
  from the emitted import lists (`esm_import_order.rs`,
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

## Open decision: the stage 2 producer

Stages 3–5 consume per-statement effect sets and do not care how they were
computed. Two producers are on the table. The choice is made on measurements
against the real specs, not on the design.

**Execution-based (today).** `facts/sync_effects/` runs the top-level
statements in source order through one abstract world, re-executes a callee's
body at each invocation, and on an opaque call runs every escaped callback to
a fixpoint. What the work so far showed:

- **Cost follows execution, not code size.** A statement costs about the square
  of the escaped-callback pool once any operation in it is refused, and after a
  chunk's first opaque call refusals are everywhere
  (<analysis_performance.md> § Superlinear callback pools). Constant-factor
  work bought 10–30%; the target needs 10–25×.
- **Coverage is all-or-nothing per statement, and havoc is global.** A
  statement the kernel cannot finish is `incomplete`, and one unknown call
  gives the whole statement global havoc, so it conflicts with every pure
  definition it is swapped with. On the public corpus split one module per
  atomic group, the effect verdict leaves 33–42% of swapped pairs unproven,
  all of them havoc-only, and about 80% of the modules fall into one
  component; no subset of coverage rows frees a module, and removing the
  callback-pool mechanism would leave 85–99% of those pairs. The unknown
  calls are mostly bundled vendor code behind CommonJS interop. The legacy
  gate accepts the same assignment and the emitted tree runs: it keeps impure
  statements in mutual order and lets pure definitions move
  (<../debug/2026_11_10_excalidraw_module_blockers.md>).
- **"Unknown" is not structurally tied to "escaped".** A call through an
  unknown value reaches only escaped functions, so every site that widens a
  value to unknown must also escape what it dropped. An audit found sites that
  do not, with accepted splits that diverge under Node
  ([§ Known open problems](#known-open-problems)).

**Summary-based (built, retired).** One inclusion-based points-to and call-graph
fixpoint over the chunk, flow- and path-insensitive inside function bodies;
one effect summary per function, computed once and instantiated at call sites;
one escaped set that every unmodelled flow feeds and every unknown call may
invoke. A statement's effect set is its own code plus the summaries of what it
may call at initialization.

- **Expected gain.** Cost near-linear in code size. Every statement gets a
  complete effect set, so there is no `incomplete` class and no
  attempt-and-rewind. "Unknown means escaped" holds by construction.
- **Known cost.** Less precision: no strong updates inside functions, no
  decided branches, context-insensitive unless refined. Each lost proof costs
  module granularity in a spec.
- **What decides it.** The pairs that matter are a statement making an opaque
  call swapped with a pure definition. The producer must answer which
  functions could be invoked as of that statement (escaped by statements up
  to it in source order, plus its own callees) and whether any of them
  touches the definition; one whole-chunk escaped set would reproduce the
  single component. After that, vendor callees in other chunks are the next
  precision bucket (whole-program constraints).
- **Unknown.** Whether the remaining loss matters for the real specs. A
  report-only prototype under `x/` measures it: for each real spec's emitted
  order, how many authored modules would have to be merged; the same for the
  per-atomic-group stress assignment on the public corpus, joined pair by
  pair with the current engine's result; and a cross-check that it reports
  every conflict today's analysis models and refuses the audit's unsound
  shapes.

**Statement-class (measured candidate).** The points-to form above was built
report-only and retired: 65–120 s and 3–5 GB per large chunk, and it did not
converge on the largest one. What replaced it under `x/summary_effects/` does
no value tracking. It classes each top-level statement as a pure definition,
contained (allocates and touches only what it allocates), confined to one
target, or impure; keeps impure statements in mutual order; and lets an impure
statement conflict with a definition only if the definition is reachable by
mention from something that escaped earlier in source order. Program-wide
derivations (allocators, bundler helpers, string-constant exports, a census of
writes to language-defined globals, split by constructor side and prototype
side) and the spec's declared premises supply the rest. Measured on each
spec's emitted order, with only the premises the spec declares:

| Spec                                  | Largest authored component        | Time per chunk |
| ------------------------------------- | --------------------------------- | -------------- |
| Public corpus, committed spec         | none (0 unproven pairs)           | ~2 s           |
| Public corpus, one module per group   | 4 and 23 modules (two chunks)     | ~2 s           |
| Private A, main chunk (2,177 modules) | 82                                | 7 s            |
| Private B, six largest chunks         | 104, 34, then 9 or fewer          | 8–10 s         |
| Private C, main chunk                 | 5, on a synchronous require cycle | 14 s           |
| Private D, E                          | none                              | seconds        |

- **Against the yardsticks.** The execution-based engine takes about 17
  minutes on private A's main chunk and its effect verdict leaves most of the
  stress assignment in one component. The cost target is met with room.
- **What the residue is.** On A, 18 impure statements tie the 82 modules; on
  B, two statements tie all 104. None of the sampled ties is order-dependent.
  Each is one of a few shapes: a read-only expression statement, member reads
  of a table that is passed around but never mutated, a nested literal read,
  the TypeScript enum form, a formatter called with literals, an imported
  namespace-export helper, and a handful of local callees that need a declared
  premise.
- **Soundness evidence so far.** It refuses all 129 Node-divergent fixtures
  and all 79 orders the current engine refuses in the e2e suite; every
  acceptance rule added has a refused sibling that Node shows order-dependent.
  It has not had an independent review, and it runs only as a report.
- **Preconditions it relies on.** No top-level `await` in the program; no
  direct `eval` or `with` in a split chunk; every static import resolves
  (unseen dynamically imported chunks run in a later job); a chunk on a
  synchronous import cycle gets no cross-chunk derivations.

**Decision rule.** Adopt the statement-class producer if, with bounded
refinements (shape rules with refused siblings, existing premises as axioms),
the real specs keep their modules and the cost target is met. Otherwise keep the execution-based producer: bound havoc reach,
model the vendor interop callees, apply the fewer-opaque-events changes and
the monotone delta (<analysis_performance.md>), and finish the kernel
migration.

**The half-migrated state.** `devel` runs two verdicts (the legacy gate
decides, the effect verdict reports) and two evaluators (the flat lane and
the kernel, joined per statement by attempt-and-rewind), with old paths kept
because the legacy gate reads them. The unsound shapes found so far sit in
those seams. Adopting the statement-class producer ends the migration by
replacement: its sets are complete for every statement, so the flip needs no
coverage rows, and the flat lane, the kernel lanes, attempt-and-rewind and
the legacy gate are deleted after it. Keeping the execution-based producer
means completing the migration into the kernel first.

**Unchanged either way.** Parse, spec resolution, the loader model, order
search, swapped-pair validation, structural placement obligations, emit, the
premise surface, and the Node-differential e2e suite, which is the acceptance
suite for whichever producer runs.

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
- <analysis_performance.md>: stage 2 cost and the CI budgets.
- <native_receiver_identity.md>, <purity_analysis.md>,
  <runtime_validation.md>: analysis precision and probe work within stage 2.
- <relational_selectors.md>, <automated_spec_workflows.md>,
  <module_proposals.md>: the spec surface and stage 6.

## Known open problems

Historical evidence: <../debug/2026_10_31_tana_scc_pessimism.md> and
<../debug/2026_10_31_pure_call_heap_frontier.md>. Current gate outcomes require
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
