# Analysis performance

Prioritize source-grounded reductions in retained analysis state when their
scope and lifetime are clear. Use fresh `-c opt` profiles when a CPU hotspot or
the value of an uncertain optimization needs attribution; profiling is not a
prerequisite for an obvious, bounded memory improvement. Older replay rankings
predate the current interpreter and effect-transfer work, so they cannot rank
current work. Earlier callback-scale and other measured mechanisms are in
<../docs/lessons_learned/performance_traps.md>.

## Remaining work

1. Prefer bounded memory-lifetime improvements whose mechanism is clear from
   the current code. Compare complete output evidence for the exact source,
   engine, and spec identities; do not claim a cost reduction from code shape
   alone.
2. If repeated lazy-effect promotion is shown to remain hot, assess an
   outcome-level memo for already-promoted owners. Its invalidation must follow
   every effect drain and fallback merge; the current no-op check in
   `promote_top_effects` avoids adding effects already held by the same outcome.
3. Reconsider parallel callable-capability preparation only if fresh evidence
   makes its CPU benefit worth the memory tradeoff. A prior experiment reduced
   one stage from 11.4 s to 7.4 s while raising another workload's peak RSS from
   4.43 GiB to 5.45 GiB; the current preparation loop is sequential. Any new
   parallel design needs an explicit bound on retained execution indexes.
4. Revisit value-flow watched-key inserts, value unions and allocation;
   main-chunk interpreter and promotion costs; or SWC hygiene-lock contention
   when a fresh profile would change which candidate to pursue. Keep
   profile-backed authoring and lookup work in <../TODO.md> as the dispatch
   queue. A previous event re-evaluation experiment reduced reads but cost more
   overall through extra join sets and records; reopen it only with new
   evidence.

## Superlinear callback pools

Target: a large private consumer corpus analyzes in under 4 minutes, ideally 2. Today the largest takes about 47 min and the next about 10 min, mostly
single-threaded on 6 vCPUs.

Public proxy: the Excalidraw app build with roughjs inlined (commit
`6114b352`). It takes about 270 s on a 4-core VM, 99% of it one chunk's serial
analysis. Working targets: inlined shape ≤ 20 s, split shape (`devel`) ≤ 10 s.
Measurements are in <../perf/analysis_serial_phases.md> § Cost model.

### Cost model

Per owner, an opaque call runs the possible-callback pool:

- passes per owner grow with the pool (about 1.2 passes per pooled callback);
- each pass costs time proportional to the pool.

So an owner costs ∝ pool², and a chunk costs ∝ (owners making opaque calls
while the pool is large) × pool². In the proxy, 162 JSX icon constants run
behind a pool of about 350 escaped roughjs and editor callbacks, and take 93%
of the chunk.

The inner cost is bounded fallbacks (about 870 per heavy owner), each walking
callback reach and calling `unsettle`. 99.8% of the `unsettle` calls change
nothing.

### Options

Ranked by expected speedup per unit of precision lost. Representational
options lose none and must keep `.analysis` bodies byte-identical.
Over-approximating options must also show, with the `DEBUNDLE_TV_AUDIT=1`
base-vs-head comparison over the e2e suite, that no refusal turns into an
acceptance, and must count every acceptance lost.

1. **Memoize the reentrant-pool fallback within an owner: measured, not
   worth building as the main lever.**
   - An opaque call made inside an active pool member takes one
     `bounded_invocation_fallback` over the whole pool (`reentrant_pool`,
     `RecursiveInvocation`, global reach).
   - On the proxy: 215,105 fallbacks, taking 96.7 s of 288 s of owner time
     (34%), so a perfect memo gives at most about 1.5×.
   - 90% of fallbacks repeat an earlier revision-keyed key, carrying 88% of
     fallback cost. 98.7% add no new possible value.
   - Revisions stayed unchanged between same-owner fallbacks 93% of the time,
     while `ptr_eq` on the collections the walk reads held only 61% of the
     time. So world state changes without bumping the revisions, and a sound
     memo must validate those reads, which lowers the hit rate further.
   - The other 66% of pool cost is member invocation for opaque calls made
     outside an active member, which this memo does not touch.
2. **Promotion worklist per owner** (representational).
   - `promote_deferred_callbacks` re-promotes the same deferred set (about 228
     per heavy owner) per opaque call.
   - Key each promotion by (callback, world reads validated by `ptr_eq`, the
     scheme the export-surface memo uses), and promote once per state change.
   - Expected 2–5× on what remains after option 1.
3. **Incremental `unsettle` frontier** (representational). Per-element
   validation, as recorded in the perf note. At most 1.1× now (7% inclusive);
   do it after 1 and 2 only if it rises.
4. **Havoc-region summary** (over-approximating).
   - Once a pool's reachable heap is fully unsettled and its members are
     escaped, later opaque calls with a subset pool apply a cached "already
     havocked" result instead of iterating.
   - Precision lost: callbacks lose per-call-site ordering refinements after
     the first havoc.
   - Fallback if 1 and 2 fall short of target; expected ≥ 1's factor
     regardless of pool structure.
5. **Model the CommonJS interop wrapper: measured, no gain.** Hand-resolving
   the wrapper, or even routing every JSX call to a stable top-level
   function, leaves all 160 heavy owners invoking the pool
   (<../perf/analysis_serial_phases.md> § Interop wrapper: not the cause).
6. **Intra-chunk parallelism** (after 1 and 2).
   - Root execution is a source-order fold that threads one `World`; owners
     cannot run concurrently without speculation.
   - Within an owner, a pool pass invokes members sequentially (Gauss–Seidel).
     A Jacobi formulation would run members from one state and join the
     results in member order, so ids and merges stay deterministic. It reaches
     the same fixpoint with different iteration counts, so its bytes must be
     re-verified.
   - Other parallel candidates: callable summaries, report building.
   - Honest expectation on 6 cores: ≤ 2× on what remains, because passes are
     short after 1 and 2 and joins serialize.

### Monotone delta for opaque-call pools

Within one top owner, after an opaque call's pool invocation reaches its
fixpoint, a later opaque call needs to process only what changed since:

- newly escaped values, such as its own new arguments (a mean of 1.3 new
  values per fallback on the proxy);
- newly reachable owners;
- rebinds;
- heap writes that add edges from already-havocked values.

Everything the earlier invocation applied is monotone and still holds.

Two ways to find heap deltas:

| Approach                                           | Cost                                                                                       | Risk                                                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Per-owner write log                                | An append per heap write: properties, accessors, cell stores, descriptor maps, dense slots | Every write site must log; one missed site is unsound                                            |
| A write to an unsettled object is a new delta root | One membership probe per write, against `unsettled_objects` (and havocked cells)           | Same write-site coverage, simpler data; re-expands the whole written object rather than one edge |

Prefer delta roots, with every heap write routed through the
`ObjectSurfaceStore`, `LexicalCells` and `DenseArrays` methods, so coverage
is checked in a few functions instead of at call sites.

Reset the per-owner frontier, and run the next opaque call in full, on any
operation that can narrow state the frontier assumed widened:

- `LexicalCells::store_declared`, which replaces a cell's values on its
  declaring activation;
- dense-array slot replacement, which is a strong update;
- a `writable_prefix` proof, `InProgress → Proven`;
- world restore or join: `checkpoint_root_attempt` rewinds and alternative
  joins.

Everything else the pool touches only accumulates within a prefix:

- `prefix_values` (extend);
- `prefix_object_properties`;
- `applied_accessors`;
- `unsettled_objects`;
- revoked results;
- the escaped and exported value sets;
- reachability owners.

Before building, list every write method in the PR and classify each as
accumulating or narrowing.

### PR order

| Step | PR                                                                    | Gate                                           |
| ---: | --------------------------------------------------------------------- | ---------------------------------------------- |
|    1 | Monotone delta for opaque-call pools (below), with option 2 folded in | identical bytes, or the output-changing review |
|    2 | Budget (below)                                                        | —                                              |
|    3 | Re-measure; then option 4 if still short                              | TV audit                                       |
|    4 | Option 3, `unsettle` frontier, if still hot                           | identical bytes                                |
|    5 | Option 6, intra-chunk parallelism                                     | identical bytes                                |

### Budget

Add the inlined shape as a second, postsubmit Excalidraw corpus target, so the
superlinear path stays exercised. Record `DebundleAnalyze` wall time for both
targets from BuildBuddy execution metadata.

- First, alert when a target exceeds 1.5× its trailing median.
- Once options 1 and 2 land, fail when the split shape exceeds 20 s or the
  inlined shape exceeds 60 s on RBE.
- Tighten toward the 10 s and 20 s targets as they are reached.

## Required evidence

- When attribution or a cost claim calls for profiling, use an optimized
  binary, pre-staged inputs, and a real profiler as described in
  <../README.md#bazel-integration-and-profiling>.
- For each change, preserve byte-identical dependency edges, atomic groups,
  precision losses, and gate verdicts on the validation inputs.
- Keep CPU, wall time, peak RSS, and parallel durations distinct. A narrow
  profile establishes a mechanism, not aggregate savings.
