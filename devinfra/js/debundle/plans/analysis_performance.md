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
2. For repeated lazy-effect promotion, follow the dependency and semantic-record
   design in <cross_chunk_parity.md>. Share summaries before read/rebind expansion
   and distinguish idempotent may-effects from ordered observations.
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

## Callback-analysis cost and budgets

The algorithm, bounds and implementation sequence have one owner:
<cross_chunk_parity.md>. Use its finite-domain/dependency design rather than
adding an owner-local pool memo, a counted fallback ledger or a parallel replay
loop. Source-level sharing must reach effect consumers; a faster heap walk
alone does not eliminate repeated source promotion.

Existing phase measurements are in <../perf/analysis_serial_phases.md>; the
current cross-chunk investigation and reproducible workloads are in
<../debug/2026_11_15_maintained_spec_resource_limits.md>. They establish
mechanisms for their exact inputs, not a current canonical completion time.
Do not use historical percentage ceilings to rank a changed implementation.

Outstanding performance work:

1. Use generated workloads varying owner count, callback count, shared overlap,
   graph depth and relevant mutations independently. Check structural work
   against the algorithm's bounds as well as completed CPU/wall/RSS runs.
2. Complete matched analyses on canonical Excalidraw, the upstream-icons
   candidate, private Claude CLI and Tana, with the normal gate and runtime
   evidence. Separate source-analysis cost from ESM scheduling/search.
3. Retain the working latency targets: under four minutes, ideally two, for
   the largest private consumer; ten seconds for split Excalidraw and twenty
   for its inlined shape on the reference machine. These are targets, not
   verified results for the new algorithm. Establish the matching baseline
   before adopting machine-dependent CI thresholds.
4. Add the inlined Excalidraw shape alongside the split corpus target so both
   analysis shapes remain exercised. Use BuildBuddy execution metadata for
   completed actions, and compare repeatable distributions before tightening
   a postsubmit regression budget.

## Required evidence

- When attribution or a cost claim calls for profiling, use an optimized
  binary, pre-staged inputs, and a real profiler as described in
  <../README.md#bazel-integration-and-profiling>.
- For a representation-only change, preserve semantic facts and external
  outcomes. For a domain/comparator correction, review every fact, precision and
  verdict difference against causal CLI evidence; byte identity is not the
  correctness oracle for a deliberately changed semantic record.
- Keep CPU, wall time, peak RSS, and parallel durations distinct. A narrow
  profile establishes a mechanism, not aggregate savings.
