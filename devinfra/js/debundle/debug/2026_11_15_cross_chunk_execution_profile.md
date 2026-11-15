# Shared execution: foreign callback fallback dominates Analyze

This measures the incomplete cross-chunk implementation while executing the
[cross-chunk parity plan](../plans/cross_chunk_parity.md). It is a diagnosis,
not evidence that the icon extraction passes.

The optimized canonical Excalidraw Analyze action remained active for more than
26 minutes. Read-only BuildBuddy execution inspection showed that it was
executing rather than waiting for an executor. The earlier split-corpus
[baseline](../perf/excalidraw_roughjs_analyze.md) completed in 39–49 seconds;
these are different engine revisions, so this comparison identifies a regression
to investigate rather than a controlled final benchmark.

## CPU profile

The optimized CLI and complete native source/package inputs were materialized
using RBE in invocation
[`bc691c7a-401d-4b84-8326-4cd9af341994`](https://app.buildbuddy.io/invocation/bc691c7a-401d-4b84-8326-4cd9af341994).
The frozen CLI SHA-256 was
`fd2f69c6606c92fc3bba7bcd7ed0c218f3242b1147d2dd64920572927bd4d714`.
The canonical analysis projection includes the proposed upstream icons module.

`perf_wrapper.sh` recorded a bounded 90-second `debundle analyze` run at 49 Hz
with DWARF callgraphs. It collected 15,459 samples of `cycles:u`, no lost samples,
on a Linux 6.18.55 host with an AMD Ryzen 9 9950X3D. Analysis did not complete
within the diagnostic window. Its partial execution proves no acceptance result.

| Children samples | Stack                                       |
| ---------------- | ------------------------------------------- |
| 82.02%           | `Executor::retain_foreign_callback_effects` |
| 81.38%           | → `refuse_unavailable_runtime_source`       |
| 81.36%           | → `promote_foreign_record`                  |
| 64.38%           | → `promote_top_effects`                     |

The largest self costs were `Atom::cmp` (23.85%), `memcmp` (21.41%),
`promote_top_effects` (11.54%), and `Atom::as_str` (7.75%). The callgraph, rather
than the comparison symbols alone, identifies the cause: a retained foreign
callback unconditionally takes the unavailable-source path even when its
canonical prepared source and instantiated module bank are present. That path
promotes whole-source reads/rebinds and unsettles reachable values repeatedly.

The full profile and rerunnable command are under the task scratch directory
`/tmp/agents/2026_11_15/cross-chunk-parity/perf-b64/profile/`; these ephemeral
artifacts are not a durable final benchmark.

## Required repair and validation

Available prepared callbacks must use the ordinary source-aware promotion
closure over their actual captures, live cells, and heap references. Missing
source/bank information still requires a conservative refusal. Merely caching
or deleting the wide effects would lose callback timing obligations. The
[runtime-source investigation](2026_11_15_runtime_source_identities.md) records
the separate imported-read and fallback-result transport soundness defects.

After the semantic repair, rerun normal canonical Analyze and the icons gate,
then compare completed RBE action CPU/wall/RSS and a matching native profile.
Browser and maintained-spec validation remain separate acceptance requirements.

## Finite promotion and the local quadratic reproduction

Finite source-aware promotion removes the incorrect unavailable-source path.
Its next bounded profile still spent 85.80% of inclusive samples in
`retain_foreign_callback_effects`, mostly cloning and filtering immutable
source read sets. Reusing the prepared `synchronous_top_effects` views, with
imported bindings included, removed that repeated filtering. A third bounded
profile attributed 43.57% to foreign promotion and 28.59% to
`walk_callback_values`. These overlapping shares describe where samples moved;
none of the capped runs establishes completed throughput or an icon gate pass.

[PR #16652](https://github.com/agentydragon/ducktape/pull/16652) independently
reproduces repeated callback traversal without imports or extraction. Its
[measurement receipt](2026_11_15_callback_registration.md) records roughly
quadratic scaling for closures with an attached reader. Its runtime fixtures
and generator are included in this integration for matching local/imported
checks. The original two fixtures passed on this engine in invocation
[`bc89e724`](https://app.buildbuddy.io/invocation/bc89e724-fba8-49be-8236-624ffd09f4ab);
this is behavioral evidence, not a performance fix.

A proposed per-operation foreign-root batch is not applied: class capability
promotion can invoke captured superclass bodies, changing live references after
another root was expanded. A shared visited set without revisitation is not a
sound general optimization. Any incremental reachability or transfer reuse must
cover changes to module cells, captures, heap edges, and those executions; a
cached whole effect set cannot substitute for that dependency contract.

## Immutable source promotion census

The next change lazily retains the reached source owners and their union of
top-level reads/rebinds for each source owner and completeness mode. It stores
no runtime values or execution effects. Each use still resolves current module
cells, follows actual retained callbacks, promotes class capabilities, records
the current scheduling owner, and retains the active allocation identity.
Rows without a common scheduling owner keep their individual attribution.

All six focused suites passed in
[`854a6687`](https://app.buildbuddy.io/invocation/854a6687-019a-48b3-b4bc-cead8511ae37),
including callback publication, bounded fallback, intermediate availability,
imported array iteration, and all four local/imported registration controls.
The expanded operation-family matrix separately passed all 33 cases in
[`55b26a40`](https://app.buildbuddy.io/invocation/55b26a40-cb04-4725-8584-a467aefa1797).

The optimized CLI from
[`db21bab7`](https://app.buildbuddy.io/invocation/db21bab7-00bd-461c-9098-952c1e76c52a)
has SHA-256
`1f50e6f3f664a35423402306538c4bbfca23ae03227926cc4389f23446c83183`.
Its same-input 90-second profile captured 15,670 samples without reported loss;
Analyze still did not complete. The middle and late six-second stack slices
contain `retain_foreign_callback_effects` in 97.84% and 97.24% of samples,
respectively, and `walk_callback_values` in 40.50% and 38.25%. These are
unweighted slice counts, not a completed throughput comparison. The whole-run
report has mostly shallow stacks with this binary, so its inclusive symbol
shares are not comparable with the earlier profiles.

## Current bounded canonical check

The optimized native CLI and complete Excalidraw inputs were rebuilt in
[invocation `fb9e3aa7`](https://app.buildbuddy.io/invocation/fb9e3aa7-df1b-4fc4-9786-a5bc1c3318fb)
after the canonical initializer completion and external-import changes. Fresh
projection succeeded. With four Rayon threads, analysis reached the 120-second
diagnostic cap without writing an analysis artifact: 120.19 seconds wall,
351.11 seconds user CPU, 2.85 seconds system CPU, and 2,438,652 KiB peak RSS.
GNU time and `perf stat` recorded the run; its command and outputs are in the
task's `canonical-current/` scratch directory.

This establishes that canonical analysis remains blocked on the current
implementation. It establishes neither an icon extraction result nor a
throughput improvement over devel. The input and engine differ from earlier
measurements, and the run did not complete.

After the immutable imported-name index, the optimized native inputs were
rebuilt in
[`2371b8f6`](https://app.buildbuddy.io/invocation/2371b8f6-691b-4653-a3cf-228bc416b76c).
Fresh projection succeeded, but analysis with `RAYON_NUM_THREADS=4` reached a
600-second cap without writing an artifact: 600.12 seconds wall, 1,604.33 seconds
user CPU, 1.92 seconds system CPU and 1,866,868 KiB peak RSS. GNU time and
`perf stat` outputs are retained under the task's `canonical-index/` scratch
directory. This longer run still establishes no canonical acceptance or
completed performance comparison. The remaining callback-promotion bottleneck
is separate from the imported-name lookup improvement.
