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

1. **Memoize the reentrant-pool fallback within an owner** (representational
   if bytes hold).
   - Today, an opaque call made inside an active pool member does not re-run
     the pool. `invoke_callback_pool_once` sees an active member
     (`reentrant_pool`) and takes one `bounded_invocation_fallback` over the
     whole pool (`RecursiveInvocation`, global reach).
   - A heavy owner makes about 850 such nested calls with near-identical
     requests. Each re-walks the pool's reach and calls `unsettle`, and 99.8%
     of those calls change nothing. That repetition is the pool² term.
   - Key the fallback like `InvocationSignature` does: pool, reason, reach,
     top owner, assumption, and `revision.{prefix, reachability}`. Also
     validate the non-world execution state the fallback reads, and check by
     `ptr_eq` the world inputs the revisions are known not to version.
   - On a hit, replay the cached outcome and rebinds. The fallback's world
     effects are idempotent and still hold.
   - What a hit drops is only per-site diagnostics: each nested call site and
     its argument list, which feed the first-class-publication record
     (`or_insert`) and fallback input paths.
   - Measure the hit rate before landing. Expected 5–20× on the proxy.
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
5. **Model the CommonJS interop wrapper instead of treating it as opaque**
   (no precision loss if derived).
   - In the proxy, `f = ca(u1())`. Here `u1` is esbuild's `__commonJS` wrapper
     around the JSX runtime's `module.exports`, and `ca` is `__toESM`,
     imported from esbuild's shared runtime chunk.
   - `__toESM` builds a namespace object with
     `defineProperty(…, {get: () => b[d], enumerable: …})` for each own key of
     the exports. So `f.jsx` is an accessor read through an imported helper,
     and each icon makes opaque calls.
   - **Asserted form.** A spec premise selects the wrapper call and asserts:
     its property reads forward to the factory's `module.exports` own data
     properties; the getters run no application code; and the exported
     functions do not synchronously invoke callbacks they did not receive as
     arguments. Audit it by reading the helper and factory source.
   - **Derived form (preferred).** Recognize the exact esbuild runtime helper
     bodies (`__commonJS`, `__toESM`, `__copyProps`) structurally, including
     across chunks through the program summary. Then model `__toESM(x)` as a
     namespace whose property reads forward to `x`'s own properties. `f.jsx`
     resolves to the factory's function, and the interpreter analyzes it like
     any local callable; the JSX runtime only reads props and allocates.
   - Soundness rests on matching the helper bodies exactly. A mismatched body
     stays opaque.
   - React-style consumer corpora repeat this shape for every interop import,
     so the derived rule can matter more than the proxy suggests. It removes
     the opaque calls themselves, not just their cost.
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

### PR order

| Step | PR                                            | Gate                                           |
| ---: | --------------------------------------------- | ---------------------------------------------- |
|    1 | Option 1, nested-pool subsumption             | identical bytes, or the output-changing review |
|    2 | Option 2, promotion worklist                  | identical bytes                                |
|    3 | Budget (below)                                | —                                              |
|    4 | Re-measure; then option 4 or 5 if still short | TV audit                                       |
|    5 | Option 3, `unsettle` frontier, if still hot   | identical bytes                                |
|    6 | Option 6, intra-chunk parallelism             | identical bytes                                |

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
