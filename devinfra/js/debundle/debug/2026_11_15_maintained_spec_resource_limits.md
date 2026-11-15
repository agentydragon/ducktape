# Maintained-spec resource limits

The shared execution candidate is not yet validated on the maintained large
specifications. Invocation
[`0f356891`](https://app.buildbuddy.io/invocation/0f356891-b7b8-48f0-8fa9-5917b0f44778)
built the private repository at `c221d4bd9c3f2772a24b09c752705052ac585d14`
against Ducktape `e68222c09c961c639286d5234448b886da7a9bdf`. The private
worktree used an absolute module override and its existing Cargo source-label
patch; neither repository's primary checkout or dependency pin changed. The
temporary label patch was removed after the build finished.

The requested targets were Claude CLI `2.1.292:debundle` and Tana Web
`78d928dca7:debundle_passing`. Both projections completed. Neither analysis
produced a successful result, and neither apply/gate nor output execution was
validated.

## Action evidence

Claude CLI analysis was retried six times by remote execution. Every completed
attempt reports `executor OOM killer terminated task` (status 14). The executor
estimated 7,500,000,000 bytes of memory; reported peaks ranged from
46,218,612,736 to 62,082,744,320 bytes (43.0 to 57.8 GiB). Actual execution
took 76.9 to 122.8 seconds per attempt; retries and setup account for the much
longer overall build. The build's final error-detail memory observation differs
slightly from the final attempt's usage-stat peak, so it is not an exact peak.

Tana analysis has two completed attempts reporting `command was killed:
signal: killed` (status 8), each at a reported peak of 7,630,225,408 bytes
(7.1 GiB). Their execution intervals were 431.9 and 258.7 seconds. This proves
execution started; it does not establish the exact cause of the signal. An
additional zero-timestamp `EXECUTING` metadata row is not a completed attempt.

BuildBuddy's target summary labels Tana `BUILT`, but the target-completion BEP
event contains `aborted`, not a successful `completed` event. The action
results and absence of a successful analysis take precedence over that label.
Do not report this invocation as a Tana pass.

## Bounded native profile

Invocation
[`793033cd`](https://app.buildbuddy.io/invocation/793033cd-6569-4b86-a1b2-2ef6ec05acff)
materialized the optimized CLI and the same projected CLI inputs without
rerunning the failed remote analysis. The engine remained `e68222c09c`; fresh
projection succeeded. Native analysis used four Rayon threads, an 8 GiB virtual
memory limit, and a 45-second diagnostic cap. It aborted on allocation failure
(exit 134), before producing an analysis artifact. GNU time measured the whole
profiling wrapper, including postprocessing: 44.21 seconds wall, 59.23 seconds
user CPU, 3.56 seconds system CPU, and 7,852,188 KiB peak RSS. Those timings are
not a completed Analyze duration.

`perf_wrapper.sh` recorded 2,954 samples at 49 Hz with DWARF callgraphs and no
lost samples. Inclusive shares overlap: `ExecutionIndex::collect` accounted for
29.49%, `PrototypeWrites::new` for 21.97%, and
`NativePointEffects::add_prototype_mutation` for 10.83%. The largest self costs
were `memcmp` (15.60%), allocation (11.06%), and the preflow
`PrototypeWrites::new` call (9.17%). This is a CPU profile, not heap allocation
attribution.

Source inspection identifies two repeated whole-census costs:

- `resolve_imported_callable_summaries` asks every prepared module for inferred
  Set constructions. Each request rebuilds `IntegrityFlags` and its prototype
  histogram from the same completed program census, cloning property strings
  even when that module contributes no Set proof.
- `populate_native_point_effects` copies every foreign prototype mutation into
  each module's initial effects and clones those counts for its all-effects
  summary. Interpreter initialization, widening and root-attempt checkpoints
  make further deep copies of the string-keyed map.

The histogram construction repeats work proportional to modules times census
key entries. The profile does not measure total module/mutation counts or prove
which path dominates retained heap. Raw evidence remains in the task's
`cli-oom-profile/` scratch directory; this note preserves the measured failure
and source-grounded leads without publishing private bundle source.

## Scoped next repair

Do not retry the same large analyses unchanged or raise their memory estimate
as a substitute for diagnosis. Share the immutable preflow prototype histogram
across module-specific flags; keep census closure, exact-site axioms and consumed
premises separate. For native point effects, a shared foreign baseline plus local
activation deltas can avoid copying the foreign map. A bare copy-on-write map
would still copy it on the first local activation. Foreign counts must remain
active at entry and local counts withheld until their actual activation.
Preserve duplicate/unknown-key counts, raw witnesses, native prefix observations,
and direct/saved parity. Measure retained memory and a changed native
reproduction before retrying the maintained target. Check it against the separate
[callback-promotion profile](2026_11_15_availability_trace_storage.md#remaining-callback-promotion-work).
Any cache or bounded havoc alternative must preserve read dependencies,
ordered availability observations, captured-cell invalidation and conservative
effects. The user's approved bounded precision scope permits explicit havoc
boundaries; it does not permit dropping effects to make the workload finish.
