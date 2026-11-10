# Vendor value-flow cost on the canonical web corpus (2026-10-29)

Follow-up to <pin_vs_devel_tana_2026_10_29.md> § Vendor-chunk value flow: the
vendor chunk's `prepare_cross_module_purity` took 31 s on the pinned engine
and 249 s on devel `4377a57b`. Three PRs cut it without changing any output:
#12954 (candidate reachability as a second stratum), #12955 (dense binding and
property slots), #12960 (candidate reads limited to origins that can carry
reachability). The base for every comparison below is devel `fc3ef985de`; each
PR binary is that base plus the stack up to it.

## Method

Same local harness and staged inputs as the parent note
(`/var/cache/agentydragon/debundle-perf-pin-vs-devel/`, `scripts/run_engine.py`
under GNU time `-v`, stage walls from `scripts/stages.py` markers). Engines are
`bbr build -c opt --config=nolint //devinfra/js/debundle:debundle` outputs. The
host was shared with other agents throughout (1-minute load 7–12, including
other runs of this harness), so base walls are noisier than on the quiet host
the pin was measured on; compare rows within this note only.

Identical output: for every run, all three report files
(`dependency_graph.json`, 1.17 GB; `atomic_group_conflicts.json`;
`export_reentry.json`) were byte-compared with the base run after blanking
run-specific provenance (run directory, engine path, `engine_sha256`). All
matched. Every run exits 1 with the same gate rejection as the parent note's
devel run.

## Canonical input, alternating pairs

Base and the full stack, three alternating pairs (base first in pairs 1 and 3).

| Engine | Vendor stage (s)    | Vendor median (s) | RSS at vendor end (GiB) | Whole run        | Whole-run median | Peak RSS median (GiB) | `compute_chunk_analysis` (s) |
| ------ | ------------------- | ----------------: | ----------------------: | ---------------- | ---------------: | --------------------: | ---------------------------- |
| base   | 309.3, 263.8, 237.3 |             263.8 |                    3.21 | 8:37, 7:46, 7:08 |             7:46 |                  5.38 | 191, 183, 173                |
| stack  | 60.6, 72.1, 64.9    |              64.9 |                    1.97 | 4:26, 4:42, 4:34 |             4:34 |                  4.32 | 188, 192, 186                |

Single runs per PR (concurrent with other work):

| Engine            | Vendor stage (s) | RSS at vendor end (GiB) | Whole run | Peak RSS (GiB) |
| ----------------- | ---------------: | ----------------------: | --------- | -------------: |
| base `fc3ef985de` |            195.9 |                    3.21 | 6:44      |           5.46 |
| + #12954          |            130.0 |                    2.27 | 5:37      |           4.62 |
| + #12955          |             64.5 |                    1.97 | 4:11      |           4.31 |
| + #12960          |             55.3 |                    1.97 | 4:23      |           4.31 |

## Attribution

Counts are from a local, uncommitted counting build of #12955's head
(evaluations, reads, watcher insertions and wake checks in
`IterativeInference::solve_with_premises`); seconds are the measured stage
deltas above.

| Cause                                   | Finding                                                                                                                                                                                                                                                                                                                                                                                                               |  Cost removed |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------: |
| (a) Redundant re-solving across callers | None on this input. One provider module (the vendor chunk) runs one whole-chunk fixpoint, and no other stage recomputes its value flow. Within the fixpoint, 547,800 evaluations over 373,551 event rules (1.47 per rule, at most round 17)                                                                                                                                                                           |           0 s |
| (b) String-keyed fact store             | `FlowKey::Binding(Id)`, `Property(u32, String)` and candidate rules compared identifier text in every fact, read-set and watcher B-tree; `memcmp` was 28.5% of vendor-stage samples after #12954                                                                                                                                                                                                                      | 65 s (#12955) |
| (c) Value-set breadth                   | Event evaluations average 98 reads, candidate evaluations 537, because member and call reads fan out per origin of large value sets. No widening applies: `Values` are finite origin bitmaps without constants (`plans/abstract_interpreter.md` § 6.2 widens the interpreter's domain), so widening them would change results. Reads of origins that can never carry reachability (63.8M candidate reads) are skipped |  9 s (#12960) |
| (d) Watcher and wake amplification      | Candidate rules shared the event worklist: every carrier growth re-ran each affected candidate-binding rule, re-reading all its values and re-installing a watcher per read                                                                                                                                                                                                                                           | 66 s (#12954) |

## What remains

The vendor stage is about 2× the pin's 31 s (paired median 64.9 s on a loaded
host). In #12955's vendor-window profile, about 55% of samples are the generic
worklist's ordered-map bookkeeping in the event stratum:
`solve_values_resumable` self time 29% (watcher registration and wake loop
inlined), the fact-map search 12%, read-set inserts 8%. The counts locate it:
53.8M reads register 8.9M watchers (83% of insertions re-register an existing
watcher), and 26.3M wake checks produce 174k wakes.

Options not taken here:

- A hash-indexed watcher map, read set and fact store in `inference/iterative.rs`
  would need `Hash` on the key and rule types of every inference family (none
  derive it today), and a cross-family CPU and heap comparison per
  <../docs/inference.md> § Storage and diagnostics.
- An incremental (semi-naive) formulation of member and call reads would change
  rule structure, not just storage.

The whole run is now dominated by `compute_chunk_analysis` (the synchronous
execution interpreter, about 185 s against 29 s on the pin; parent note
§ Stage attribution), which these PRs do not touch.

## Public controls

The ten captured public-control fixtures from the step 1 measurement
(`/var/cache/agentydragon/debundle-interp-step1/fixtures`) ran under base and
each PR. There were three rounds, with the engine order rotated each round,
each run on a fresh fixture copy. Emitted JavaScript was byte-identical across
all 120 runs.

| Case                                 | Exit | base wall median (s) | #12954 | #12955 | #12960 | base RSS median (KiB) | #12960 RSS median (KiB) |
| ------------------------------------ | ---- | -------------------: | -----: | -----: | -----: | --------------------: | ----------------------: |
| `cached_invocations_fallback_fanout` | 0    |                 2.76 |   2.49 |   2.35 |   2.41 |               212,816 |                 225,800 |
| `escaped_callback_read_closure`      | 0    |                 3.31 |   3.54 |   3.15 |   3.14 |               235,192 |                 221,400 |
| `exported_aliases`                   | 0    |                 0.48 |   0.47 |   0.47 |   0.46 |                57,080 |                  59,272 |
| `gen_synth_corpus` (10k, seed 1)     | 0    |                 0.78 |   0.78 |   0.79 |   0.77 |               162,584 |                 163,000 |
| `namespace_only_provider`            | 0    |                 0.29 |   0.28 |   0.29 |   0.30 |               183,520 |                 185,636 |
| `reentrant_callback_pool`            | 0    |                 0.15 |   0.14 |   0.15 |   0.14 |                53,796 |                  55,936 |
| `repeated_opaque_calls`              | 0    |                 0.21 |   0.22 |   0.22 |   0.21 |                40,480 |                  42,744 |
| `repeated_wide_value_sets`           | 0    |                 0.10 |   0.09 |   0.10 |   0.09 |                67,704 |                  69,816 |
| `repeated_writes`                    | 0    |                 0.21 |   0.21 |   0.21 |   0.21 |                82,492 |                  83,472 |
| `shared_equivalent_capture_dags`     | 0    |                 0.04 |   0.04 |   0.04 |   0.04 |                30,776 |                  33,048 |

Wall differences are within the noise bounds of
<abstract_interpreter_step0_baseline.md> § Public controls except
`cached_invocations_fallback_fanout`, which is 13% faster. Peak RSS rises by
1–2 MiB on the smallest cases, within that note's stated 2.4 MiB spread. The
two callback cases move in opposite directions after #12955: +13 MiB (6%) on
`cached_invocations_fallback_fanout` and −14 MiB (6%) on
`escaped_callback_read_closure`. Heap profiles were not taken.
