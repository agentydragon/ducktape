# Analysis performance

Reduce repeated semantic computation through shared facts and explicit
consumed dependencies. Sealed graphs should use bottom-up closure; evolving
relations should propagate additions through indexed edges. Putting a transfer
on `IterativeInference` does not establish that its work is incremental.

## Ownership

<../TODO.md#remaining-dispatch-order> owns priority and dispatch. This file owns
open analysis cost tasks and their completion evidence;
<analysis_consolidation.md> owns the algorithm and stage dependencies;
<abstract_interpreter.md> owns operation/capability migrations; and
<convergence_landing.md> owns gate-migration obligations. Task IDs below are
stable references, not a second ordered queue. Remove closed tasks after their
remaining limitations have explicit owners; keep measurements in `perf/` or the
existing performance report, not in a completion log here.

## Open analysis tasks

### PF1 — Incremental invocation targets and arguments

Open follow-up to [PR #17071](https://github.com/agentydragon/ducktape/pull/17071).
That prerequisite avoids reevaluating unchanged argument expressions; it does
not complete incremental invocation propagation. Callee-expression walks,
visits to existing targets, whole argument-origin joins into parameters, and
dependency replay still repeat on wakeups. Its work-count tests establish
argument-expression reuse, not an end-to-end Tana speedup.

Replace repeated delivery with indexed invocation/argument/formal edges and
per-fact origin deltas. A newly activated edge must receive all existing
argument origins and proof dependencies, then subsequent additions; an existing
edge should receive each new origin once. Proof-only changes must propagate
without forcing unchanged value computation. The algorithm and source-order,
absence and metadata constraints are owned by
[the consolidation plan](analysis_consolidation.md#remaining-invocation-delta-work).

Close PF1 only with:

- Deterministic work-count coverage varying targets, argument origins, shared
  consumers and cycles independently. Bound deliveries by activated edges and
  distinct origins; account separately for edge discovery, proof propagation
  and retained state, with polynomial time and space bounds.
- Real CLI `spec + bundle` tests for late targets and arguments, newly activated
  edges, self-feeding arguments, missing properties, metadata readiness and
  imported calls. Check emitted JavaScript execution or the expected refusal,
  plus fresh/saved-analysis parity; retain proof-only propagation coverage.
- Matched optimized before/after profiles on identical source/spec/package
  inputs for Excalidraw and Tana, reporting CPU, wall time and peak RSS. Keep
  capped/incomplete runs explicit; no runtime improvement is established yet.

### PF2 — Bottom-up purity propagation

Consume completed function bodies and resolved call obligations. Separate the
monotone effectfulness judgment from diagnostic-reason growth, propagate typed
proof dependencies, and materialize call-site reasons from the completed facts.
Replace repeated whole-body event scans when a callee merely gains another
reason. Preserve completion requirements, local/export policy and finite acyclic
explanations; purity remains distinct from readiness and complete heap effects.

Close with work counts for deep chains, shared callees and pure/effectful
recursion, plus CLI controls for local/imported calls and unchanged diagnostics
and dependency behavior. Charge proof propagation separately from Boolean
reachability; only the latter can claim linear graph work.

### PF3 — Sealed candidate reachability closure

Consume the completed possible-value carrier graph and candidate roots. Compile
its fixed union edges once, condense strongly connected components, and
propagate completed bitmap summaries through the DAG. Share summaries within a
component. Replace repeated accumulated-bitmap joins around cycles and their
worklist bookkeeping. Preserve absent-as-bottom results, native Set carriers,
unknown/class boundaries and typed dependencies.

Close with an independent small-graph reachability oracle, work counts for
chains, fanout, shared tails and cycles, and CLI local/imported plus fresh/saved
controls. Bound graph construction and condensation-edge work separately from
bitmap and output costs; do not call set unions constant-time.

### PF4 — Compiled capability containment dependencies

Consume source-only value expressions, callable reads and class ownership.
Compile direct native roots and containment dependencies once, including an
owner-to-callable index. Propagate capability additions through those edges.
Replace repeated structural expression walks and all-callable scans per class.
Containing a native capability must never establish native receiver identity.

Close with work counts varying class/member count, aliases and shared
containers, plus CLI native/prototype mutation controls and conservative
negative cases. Preserve source-only proof premises; imported or asserted
values cannot discharge an unproved prototype-absence obligation.

### PF5 — Dependency-driven callback propagation

Replace whole-pool replay, broad world comparisons and repeated reach discovery
for bounded callback families with shared capability/effect facts at explicit
prefix cuts. Reuse existing source-unit closures and fallback accumulation;
track heap slots, captures, publication and wildcard/absence dependencies where
needed. An empty surface that later gains a callback must wake its consumers.
The receiver-confinement family in
<analysis_consolidation.md#next-bounded-callback-slice> is one bounded entry,
and does not require finishing the entire callback engine.

Each slice must name the replay/traversal it retires and the unsupported effects
that retain conservative fallback. Close PF5 when the covered families use
indexed propagation and every remaining replay family has an explicit bounded
limitation and follow-up. Require late-edge, mutable-capture, callback-order,
reentry and prefix-availability CLI controls, work counts for unchanged/shared
reach, and retained precision-loss evidence. Never replace the dependency proof
with a whole-world revision cache or raise limits to imply progress.

## Secondary measured opportunities

- Make bounded source/index lifetime improvements when ownership is clear;
  profiling is not a prerequisite for an obvious retained-state reduction.
  Compare exact outputs and CPU/wall/RSS before claiming a measured improvement.
- Reconsider parallel callable-capability preparation only with fresh CPU
  evidence and an explicit retained-index memory bound.
- Use fresh profiles to choose among watched-key insertion, allocation, SWC
  hygiene-lock contention and authoring/lookup work. A prior event reevaluation
  experiment reduced reads but added enough join state to cost more overall;
  work-count improvements alone do not establish an end-to-end speedup.

## Callback-analysis cost and budgets

The devel-based consolidation algorithm, bounds and implementation sequence
have one owner: <analysis_consolidation.md>. Use its finite-domain/dependency design rather than
adding an owner-local pool memo, a counted fallback ledger or a parallel replay
loop. Source-level sharing must reach effect consumers; a faster heap walk
alone does not eliminate repeated source promotion.

Existing phase measurements are in <../perf/analysis_serial_phases.md>; the
production Excalidraw investigation is in
<../debug/2026_11_14_excalidraw_pipeline_performance.md>. The prototype's
measurements and limitations are in
<../debug/2026_11_10_summary_effects_prototype.md>. They establish mechanisms for
their exact inputs, not a current canonical completion time. Compare candidates
to frozen devel; an unmerged implementation is an optional third comparison.
Do not use historical percentage ceilings to rank a changed implementation.

Corpus evidence obligations apply throughout the tasks above:

- Use generated workloads varying owner count, callback count, shared overlap,
  graph depth and relevant mutations independently. Check structural work
  against the algorithm's bounds as well as completed CPU/wall/RSS runs.
- Complete matched analyses on canonical Excalidraw, the upstream-icons
  candidate, private Claude CLI and Tana, with the normal gate and runtime
  evidence. Separate source-analysis cost from ESM scheduling/search.
- Retain the working latency targets: under four minutes, ideally two, for
  the largest private consumer; ten seconds for split Excalidraw and twenty
  for its inlined shape on the reference machine. These are targets, not
  verified results for the new algorithm. Establish the matching baseline
  before adopting machine-dependent CI thresholds.
- Add the inlined Excalidraw shape alongside the split corpus target so both
  analysis shapes remain exercised. Use BuildBuddy execution metadata for
  completed actions, and compare repeatable distributions before tightening
  a postsubmit regression budget.

## Per-slice completion contract

Every PF task records:

- Its existing semantic owner, input/output facts, consumers, sealed inputs and
  complete dependency set, including absent reads and proof-only changes.
- The repeated computation and decision path it replaces. Delete a superseded
  path with its last consumer; any retained compatibility authority has a named
  removal condition. An intermediate optimization leaves a specific follow-up.
- Polynomial time and retained-state bounds, with deterministic work controls
  that distinguish new facts/edges from replay of accumulated results.
- CLI `spec + bundle` execution or expected-refusal checks, relevant fresh/saved
  controls, and exact validation results. Internal facts alone are insufficient.
- Matched optimized CPU/wall/RSS evidence as slices or bounded families land,
  rather than deferring all measurement to the final redesign. State capped
  runs and coverage limits. The goal includes shared semantic authority and
  removed duplicate work; a shared scheduler or a faster walk alone is not
  completion.

For profiling and comparisons:

- When attribution or a cost claim calls for profiling, use an optimized
  binary, pre-staged inputs, and a real profiler as described in
  <../README.md#bazel-integration-and-profiling>.
- For a representation-only change, preserve semantic facts and external
  outcomes. For a domain/comparator correction, review every fact, precision and
  verdict difference against causal CLI evidence; byte identity is not the
  correctness oracle for a deliberately changed semantic record.
- Keep CPU, wall time, peak RSS, and parallel durations distinct. A narrow
  profile establishes a mechanism, not aggregate savings.
