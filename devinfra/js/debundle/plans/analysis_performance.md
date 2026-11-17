# Analysis performance

Reduce repeated semantic computation through shared facts and explicit
consumed dependencies. Sealed graphs should use bottom-up closure; evolving
relations should propagate additions through indexed edges. Putting a transfer
on `IterativeInference` does not establish that its work is incremental.

## Ownership

<../TODO.md#remaining-dispatch-order> owns priority and dispatch. This file owns
open analysis cost and pass-standardization tasks and their completion evidence;
<analysis_consolidation.md> owns the algorithm and stage dependencies;
<abstract_interpreter.md> owns operation/capability migrations; and
<convergence_landing.md> owns gate-migration obligations. Task IDs below are
stable references, not a second ordered queue. Remove closed tasks after their
remaining limitations have explicit owners; keep measurements in `perf/` or the
existing performance report, not in a completion log here.

## Open analysis tasks

### PF1 — Incremental invocation targets and arguments

The indexed production transfer and its source-order/proof/work-count contract
are maintained in
[Source invocation origin deltas](../docs/inference.md#source-invocation-origin-deltas).
It removes unchanged callee walks, old-target replay, full formal-origin joins
and cached-read replay from the invocation path. Deterministic controls vary
targets, origins, shared consumers and cycles, and separately charge exact-result
subtraction. CLI controls cover late target/argument backfill, self-feeding formals,
nested missing properties, metadata readiness and imported calls with fresh/saved
parity. Source-ordered activation remains an explicit compatibility constraint.

Remaining before PF1 closes: matched optimized before/after profiles on identical
source/spec/package inputs for Excalidraw and Tana, reporting CPU, wall time and
peak RSS. Keep capped/incomplete runs explicit. Structural delivery bounds do not
establish an end-to-end runtime improvement. Record the completed measurements
in the existing performance report, then remove this task.

### PF4 — Matched impact profile for compiled capability containment

The structural containment implementation and its causal CLI/work-count
coverage are delivered with this change. They establish the indexed traversal
and semantic bounds, not an end-to-end speedup. The remaining work is a matched
before/after profile of the same Excalidraw and Tana source/spec/package inputs,
recording whether each workload reaches this pass and reporting CPU, wall time
and peak RSS. If a workload does not exercise the pass, state that explicitly;
do not infer an improvement from the unit work counts. Keep this task open until
those results are recorded in the existing performance report.

### PF5 — Dependency-driven callback propagation

The [sealed callback source-effect index](../docs/inference.md#sealed-callback-source-effects)
serves deferred-callback and class-member promotion. Remaining work is to
replace whole-pool replay, broad world comparisons and dynamic reach discovery
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

## Pass standardization tasks

These tasks make the existing pass families use consistent contracts. They
extend the existing inference APIs and AR4/AR5 ownership work; they do not add a
pipeline framework or require every pass to use notification-driven evaluation.

### PS1 — Explicit pass contracts and dependency stages

Graduate the existing owner inventory into one maintained pass contract table in
<../docs/inference.md>, replacing migrated inventory entries with a pointer.
For each production pass, name its semantic owner,
required/produced facts, complete policy/premise inputs, completion boundary,
downstream consumers and data lifetime. Classify its dependencies and transfers
as sealed acyclic, sealed union closure, sealed general monotone, growing
inference, or ordered execution; choose the corresponding evaluation strategy
from <analysis_consolidation.md>.

Start with the value-flow, effect/candidate closure and purity boundaries, then
extend the table as remaining pass families migrate. Close when each inventoried
boundary has a concrete producer/consumer contract and no unowned duplicate
producer. Preserve separate execution-order facts. Initial source-ordered sweep
behavior that still affects answers belongs to the owning pass contract. The
invocation activation and snapshot compatibility are now maintained in
[the invocation contract](../docs/inference.md#source-invocation-origin-deltas). A fixed
per-producer expression DAG may evaluate bottom-up during the existing sweep
while cross-producer cycles settle through shared closure; non-monotone internal
values must be recomputed exactly even when the producer's published fact
remains a join.

### PS2 — Typed sealed inputs and single-consumer transfers

The runtime prefix, general request/result, purity and invocation-memo families
still use `FactContext::read_final` / `take_final` with caller guarantees about
immutability and future readers/producers. The sealed source-semantic boundary
and owned own-property transfer contracts are in
<../docs/inference.md#sealed-inputs-and-completed-transfers>.

Continue bounded migrations of the remaining runtime families. Make growing,
sealed shared and completed single-consumer inputs distinct capabilities at the
pass boundary. Preserve typed proof dependencies, missing-evidence behavior,
explicit source/prefix order and retirement. Close each migration with the old
unchecked handoff removed, late-producer/reader misuse prevented or rejected,
and CLI fresh/saved behavior unchanged. Account for retained facts and dependency
indexes as part of AR4/AR5; no LRU or arbitrary eviction policy.

### PS4 — Pass conformance and scheduling controls

Reuse the existing inference tests and CLI fixtures to check each migrated
adapter's contract: lawful joins, monotonicity, complete watched reads including
absence/wildcards, new-edge backfill, proof-only growth and sealing. Engine tests
alone do not establish that an adapter reports all of its dependencies.

For passes declared order-independent, vary fair producer/queue order and
compare completed semantic facts plus CLI behavior. Do not permute actual
JavaScript execution order; legacy initial-sweep exceptions stay explicit until
their activation/snapshot contract is migrated. Close with shared checks used
by growing and sealed pass families, their behavioral E2Es, and work counts for
chains, cycles and shared tails. Invocation delta/frontier conformance now has
source-order, late-edge, recurring-origin and proof-only controls; its initial
sweep remains intentionally order-sensitive under the maintained contract. Standardized contracts do not require a second
test framework, stage mocks, or the same execution algorithm for every family.

## Secondary measured opportunities

- The [integrated Tana checkpoint](../perf/analysis_serial_phases.md#integrated-devel-checkpoint-2026-11-17)
  still spends about 54% of analysis CPU in fact-table/dependency operations
  and 19% in subset checks, reaching 16.3 GiB RSS before cutoff. Investigate
  repeated `FlowKey` reads/updates and the representation of bidirectional
  consumer memberships in the existing inference owner. Account for both
  directions and live relation sizes; preserve absence watchers, source-order
  activation and proof propagation. The early heap sample is not a complete
  attribution of the native peak. PF1/PF4's matched-impact obligations remain.
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

Every PF or PS task records:

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
