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

### PF0 — Observable, bounded private-consumer completion

Restore fresh projection, whole-program analysis and passing-spec application
within 300 seconds and 16 GiB for the large private web consumer. Preserve the
normal ownership, realizability, translation and emitted-runtime checks. A
timeout, OOM or analysis-only result does not meet this completion condition.
The acceptance target is the passing ownership subset. Keep the existing shared
analysis and subsetting setup; accepting the full canonical ownership assignment
is outside this performance target. The clock includes the fresh shared analysis
and passing-subset application, even though analysis also serves other specs.
Keep proprietary captures and source witnesses in the private repository.

Preserve the [guarded-snapshot certificate conditions and effect gates](../docs/inference.md#guarded-snapshot-read-certificates),
including their neutral CLI and unsafe-cut controls. Admission of an exact use
or a synthetic cut does not establish unchanged passing-subset acceptance.

Remaining work, in order:

1. Test the surviving source-use hypothesis after definite local scalar
   overwrites. A function may receive a callable argument, overwrite it with a
   primitive, and copy it through another local before a property write. Its
   flow-insensitive binding union must not make that later exact read carry
   the earlier function origin. Validate the bounded sequence-local proof in
   [Scalar local overwrites](../docs/inference.md#scalar-local-overwrites-in-comma-expressions)
   against the observed path, retaining capture/dynamic-scope barriers, Top,
   Proxy, ordinary effects and dependency subscriptions. This is a bounded
   may-flow experiment, not a purity rule or a claim that the gate rejection
   has this sole cause.

   Continue to use bounded value-source traces to choose and interpret each
   experiment. Separate value-bearing inputs from reads used only to select a
   receiver or callee; report omitted alternatives, unsupported transfers and
   exhausted capture/output budgets. A missing witness never establishes that
   a source path is absent. Small neutral CLI controls validate the observer,
   but same-engine queries and source traces cannot establish normal-gate
   acceptance or an end-to-end performance improvement.

   Run a causal experiment for one grouped cause at a time. State the predicted
   decision or provenance change, hold source/spec and the rest of the engine
   fixed, and compare the result with the baseline and relevant negative
   controls. If the predicted change does not occur, stop and update the cause
   model before proposing another recognizer. Preserve ordinary effects,
   unknown-property behavior, source-order visibility and conservative fallback.
   A diagnostic reduction may isolate a mechanism, but record its relation to
   the frozen input and reconstruct the supported shape in a neutral CLI
   regression. Never count a reduced-input gate result as acceptance of the
   unchanged passing subset.
   Run expensive diagnostic pipelines serially under a 900-second, 16-GiB,
   zero-swap cap, labeling them diagnostic-only; they do not satisfy the
   300-second completion requirement.

2. Add or extend a neutral real-CLI regression for the actual source-chain
   shape. Reproduce the current normal-gate refusal, then assert the intended
   accepted-and-emitted behavior for the valid case and retain unsafe-cut
   controls for mutation, getters, captures, writes and unsupported shapes.
   Check fresh/saved parity and emitted runtime behavior. Analysis queries,
   synthetic-only fixtures and report output cannot serve as the sole oracle.

3. Re-run the unchanged passing ownership subset through the normal gate. It
   must pass ownership, realizability, selector realization, translation and
   emitted-runtime checks without weakening those checks or changing the
   selected assignment. Keep exact-use admission, the full-gate result, and
   any surviving rejection evidence distinct; a certificate or narrower local
   decision does not close this requirement. A passing selected cut does not
   replace the unchanged full subset result. Keep private captures and source
   witnesses in the private repository.

   Every production implementation slice must also pass a matched optimized
   Excalidraw check using repeated, interleaved runs with metrics disabled on
   the same canonical spec and captured inputs; median wall slowdown must be no
   more than 10%. This is independent of private-subset acceptance. Diagnostic
   builds and instrumented profiles do not qualify.

4. Only after the unchanged subset passes, measure the final production path
   with a fresh, unprofiled optimized run. Include fresh projection, whole-
   program analysis and passing-subset application; finish within 300 seconds
   and 16 GiB. Compare gate and emitted-program behavior as well as time.
   Profiling, diagnostic instrumentation, saved analysis,
   partial phases and incomplete runs do not satisfy either acceptance gate.

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

### PF6 — Complete polynomial namespace provenance

Class-provenance preparation uses the bounded Boolean binding closure in
<../docs/inference.md#local-class-provenance>. Its shared false diamonds and
seeded/unseeded cycles have deterministic work controls; a public conditional
chain without classes or namespace contracts runs through fresh and saved CLI
analysis and emitted Node execution.

Remaining authority: `ValueExpr::may_equal_namespace`,
`ValueExpr::may_publish_namespace_inner`, `NamespaceValueFacts::class_may_publish`
and the single alias-set fact in `infer_namespace_aliases`. Replace assignment
path unfolding with a finite shared query graph and indexed alias changes.
Own-data member projection depends on the current alias set, and publication's
recursive class surface conservatively yields true while a binding provenance
cycle has no seed of its own. Preserve both meanings; never cache a recursive
in-progress false answer as a completed result. Require namespace-positive and
all-false chains, shared diamonds, cycles, projection/publication controls and
fresh/saved behavioral execution or refusal before closing PF6. Matched large
consumer profiles remain separate evidence from these structural bounds.

### PF7 — Incremental source transfers and complete delta costs

The property-write slice in [#17179](https://github.com/agentydragon/ducktape/pull/17179)
retains exact operand results and their current dependencies, activates each
receiver once, and skips unchanged operand walks. RHS growth still proposes the
shared full current result; newly reached receivers receive the current exact
RHS, including its current missing-property behavior. Immutable fields and Empty
RHS transfers retain no cache. The maintained contract and bounds are in
<../docs/inference.md#source-invocation-origin-deltas>.

Remaining costs include complete changed-expression evaluation, exact-set
comparisons, full-RHS destination joins, and binding/return transfers. The Tana
profile identifies property-update/subset paths but does not quantify how much
time belongs to each replay family.

The [combined slice measurements](../perf/analysis_serial_phases.md#class-provenance-property-reuse-and-dependency-storage-2026-11-17)
establish no aggregate runtime gain and observe higher RSS on both corpora.
Isolate the responsible slice and its retained state before claiming a memory
improvement; Tana's capped runs may have reached different propagation progress.
Charge cache slots/dependencies and physical bitmap sharing as well as index
capacity. This attribution remains shared PF7/PF8 work.

Compile or activate shared transfer dependencies and propagate newly relevant
receiver/value pairs without replaying old pairs. Preserve source-ordered
activation, exact-expression changes, durable unknowns and missing-property
watchers. Include invocation's remaining exact-set subtraction/frontier unions:
`propose_delta` currently subtracts delivered values and merges the complete
contribution into its frontier before submitting the delta. PF1's atom-delivery
bound is not a bound on these accumulated operand scans.

Preserve physical sharing as well as logical relation size. The rejected
property RHS-delta implementation made each receiver copy an old shared bitmap;
one controlled case retained five payloads where full-current fanout retained
one. The correction preserves full-result adoption. Future destination-delta
designs, including an audit of invocation's existing delta proposals, must charge
unique bitmap nodes/bytes and copy-on-write expansion across growing RHS values,
multiple receivers and shared consumers. No corpus impact has been attributed
to invocation sharing yet. Fewer delivered atoms alone are insufficient.

Close with counts for expression walks, receiver/edge visits, lookup/join work,
subtraction operands and retained frontiers, varying targets, origins, shared
tails and cycles independently. Prove bounds including nonlinear relation joins;
do not obtain fewer deliveries by adding an uncontrolled edge-by-origin history.
Require causal CLI late-receiver/value, property/return, unknown and fresh/saved
controls plus matched optimized CPU/wall/RSS evidence. Coordinate representation
changes with PF8; either slice can land independently when its contract is clear.

### PF8 — Dependency registration, storage and useful wakeups

Measure remaining registration and wakeup costs against the
[compact cell representation](../docs/inference.md#value-subscriptions).
Each repeated read still looks up its semantic key, records a cell ID and checks
forward membership. Each changed cell traverses its consumers and inserts dirty
IDs even when evaluation is already queued. Historical subscriptions can wake
events whose current exact dependencies no longer contain that key. Per-reader
hash tables, key-to-cell lookup, vector capacity and semantic-key ordering remain.
Work counters distinguish accesses from distinct edges, no-op from changing
proposals, and wake attempts from schedules. They do not account for all retained
key/payload capacity or the rest of the process's peak memory.

Use the closed source [property-producer census](../docs/inference.md#source-property-producer-census)
when measuring the remaining dependency relation. The bounded corpus
[checkpoint](../perf/analysis_serial_phases.md#property-producer-census-checkpoint-2026-11-17)
ends at different partial states; quantify completed memory/runtime and remaining
potential-writer subscriptions before attributing a full-consumer improvement.

Then reduce repeated forward membership, per-reader allocation and dirty-key
processing through explicit compiled/activated dependencies in the existing
inference owner. Compact reverse adjacency alone does not remove those accesses.
Account for both index directions, empty/small memberships, actual live edges
and allocation/capacity overhead. Retire obsolete edges only under an explicit
dependency contract: cached expressions need subscriptions even when a later
evaluation does not replay their reads. Preserve absent-key wakeups, source
ordering, new-edge backfill, typed proof propagation and completed-transfer
retirement. Proof-bearing proposals also rescan their preceding reads to register
premise edges; charge that work separately. Source-only flow already skips that
proof index, so it is not the explanation for its watcher cost.

Before grouping property notifications by receiver, measure distinct per-slot
edges, per-receiver groups and added wake fanout. Keep exact value reads and
changed-key delivery. A receiver-wide value union cannot act as a notification
for a new slot whose value was already present in that union.

Close with registration/wakeup and retained-state work controls plus identical
semantic/CLI outcomes and matched optimized profiles. Measure useful work and
total cost; fewer reads alone previously lost to extra join state. Do not add
an LRU, per-world cache or another scheduler.

### PF9 — Reuse immutable source and query preparation

Keep these bounded reuse opportunities distinct from premise-dependent analysis:

- `cross_module_purity.rs` optionally repeats all-module capability preparation
  after deriving bundle globals. For selected-value-input modules,
  `prepare_callable_capabilities` repeats the identical body-only local flow
  solve. Reuse that result across the two preparations; the execution indexes
  themselves have changed assumptions and cannot simply be reused.
- Program-backed `values_of_expr` queries in `facts/value_flow/program.rs`
  repeatedly lower syntax, rebuild coordinate mappings, evaluate completed facts
  and project origins. Some heap
  consumers query one expression separately for adjacent classifications.
  Prepare source-qualified query operations once and share their immutable
  inputs/results within an explicit lifetime; remove immediately duplicate
  queries. Cover long member chains and shared consumers, including lowering,
  projection and retained-state costs.
- Export preparation and chunk analysis repeat structural census/completion for
  overlapping modules; tracked-allocation registration and heap analysis also
  rebuild initializer metadata. Share syntax-derived preparation first. Reuse
  completed semantic results only for identical policy, argument and flow inputs.
  Preserve source-location attachment, SWC identity and source-release boundaries.

The program-wide value flow already solves once, is shared with chunk completion
and moves completed maps into its indexes. Do not recreate those completed
migrations or substitute the richer program flow for the earlier local contract
without a semantic argument. Close each reuse slice with its duplicate producer
removed, explicit consumers/lifetime, CLI fresh/saved coverage and measured costs.

### PF10 — Loader closure staging and request activation

`loader_closure.rs` reconstructs complete neighbor reach sets on each wake.
Each loader-window verdict also recomputes fixed forward/reverse static closures;
its inner growth loop clones the window and scans every request, including
already-counted requests. Separate completed reachability/static preparation from
verdict evaluation where the semantic contract permits it, and index request
activation by newly relevant chunks rather than repeatedly scanning the corpus.
Retain host premises, execution-window distinctions and failure witnesses.

Close with chains, cycles, multiple targets and nested-load CLI controls and work
counts distinguishing final reachability size from repeated set reconstruction,
request scans and verdict reevaluations. Attribute real corpus use before claiming
Tana savings; this is a source-audit finding, not a measured dominant hotspot.

### PF11 — Receiver-flow aggregate replay and fallback restart

Receiver flow in `facts/sync_effects/execution_index.rs` has separate binding,
assignment, return-site and callable keys, but reconstructs full assignment/return
aggregates on wakes. For unresolved
capture-connected callable summaries, `close_receiver_flow` rebuilds and reruns
the whole closure with fallback seeds. Preserve the completion boundary and
conservative widening; reuse settled dependencies or compile aggregate transfers
so only affected work repeats. This path is conditional on selected value inputs.

Close with bounded aggregate/second-stage work counts, cyclic captures and
late-fallback CLI controls, and retained precision-loss evidence. A quiescent
missing result cannot become a safety certificate. Measure reach and cost on the
corpus before ranking this above PF7/PF8.

### PF12 — Remaining capability containment payload work

In `facts/sync_effects/execution_index/prototype_properties.rs`, the compiled graph
visits affected ancestors once per change batch, but dirty containers still rebuild
full child/root aggregates. Terminal prototype-call
argument queries still use recursive `contained_roots`, which invokes a recursive
root query and then descends the same object fields again; deeply nested object
arguments permit quadratic source visits. Reuse a completed structural projection
or a single traversal returning the required summaries. Preserve exact internal
alternatives that may shrink; do not treat every internal value as an append-only
set merely because published capability facts grow.

Close with depth, width and repeated independent alias-growth controls that
charge child/set operands as well as affected-node counts, plus causal CLI
capability escape/refusal cases. PF4's matched impact obligation remains separate;
its current batched graph controls do not establish arbitrary multi-batch cost.

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

## Measurement and secondary opportunities

The [integrated Tana checkpoint](../perf/analysis_serial_phases.md#integrated-devel-checkpoint-2026-11-17)
spends about 54% of analysis CPU in fact-table/dependency operations and 19% in
subset checks, reaching 16.3 GiB RSS before cutoff. PF7/PF8 own the corresponding
algorithm and representation work. PF6 and PF9–PF12 are source-audit findings;
their corpus impact remains unmeasured. PF1/PF4's matched-impact obligations stay
open. Preserve the efficient existing strategies: SCC/DAG candidate/source-read
closure, changed-callee Boolean purity propagation, affected capability ancestors,
shared program flow and consuming ordered runtime transfers. Bitmap payloads and
dense output relations still have costs; these are not all linear-time analyses.

- Native-integrity census uses one broad growing root fact, but currently has
  only 16 intrinsic families. Its bounded wakeups rank below unbounded relation
  replay; do not treat every use of the general worklist as the same problem.
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
<../debug/excalidraw_pipeline_performance/README.md>. The prototype's
measurements and limitations are in
<../debug/summary_effects/README.md>. They establish mechanisms for
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
