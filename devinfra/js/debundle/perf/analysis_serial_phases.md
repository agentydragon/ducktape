# `DebundleAnalyze` serial phases

Why the analysis action averages so few cores on large inputs, and what the
serial stretches spend their time on. Engine: public revision `f4ccff2f`; the
profiled code paths are unchanged at `e22674c9`.

## Remote executor allocation (large web corpus)

From `bb execution get <id> --output=json` (`schedulingMetadata`,
`usageStats.timeline`) and the Firecracker VM log for the five
`DebundleAnalyze` actions of one private build:

- Every action asks for `EstimatedComputeUnits=3`, which it inherits from the
  `rbe_linux_x64` platform in the root `BUILD.bazel`. Corpus-level
  `exec_properties` set only memory or disk.
- The resulting task size is 3000 mCPU. BuildBuddy nonetheless boots a 6-vCPU
  VM (`"vcpu_count":6`) with a 6-CPU cpuset and a 30-CPU cgroup quota.
- Rayon is left at its default, 6 threads, and parallel phases run at 5–6
  cores.
- The tool is `-c opt` in the exec configuration: `opt-level=3`, no debug
  assertions, 16 codegen units, no LTO.

The share of wall time at each core count, from the 0.5 s CPU samples:

| Input                            |   Wall | CPU s | At ~1 core | At 5–6 cores |
| -------------------------------- | -----: | ----: | ---------: | -----------: |
| Large web corpus (807 chunks)    | 2383 s |  5606 |        70% |          26% |
| Multi-chunk CLI                  |  294 s |   431 |        80% |           9% |
| 44-chunk app                     |  250 s |   417 |        73% |          15% |
| Single large chunk (desktop app) | 2824 s |  2870 |       100% |           0% |
| Single large chunk (web app)     |  580 s |   608 |        99% |           0% |

The 807-chunk timeline has three phases: 0–2 min serial, 2–14 min parallel at
about 5.4 cores, then a 26-minute serial tail. Even an ideal 16 cores would
only bring 2383 s down to about 1910 s. Requesting more CPU does not address
the tail.

## Corpora and commands

Optimized binaries come from `bbr build -c opt
//devinfra/js/debundle:{debundle_analyze,debundle_project_analysis}`. For
callchains, a second analyzer build adds
`--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes`:
DWARF unwinding of the stock binary recovered multi-frame stacks for only 29%
of samples.

Each input is projected once with `debundle_project_analysis --spec spec.json
--out proj`, then profiled with:

```sh
PERF_CALL_GRAPH=fp PERF_RECORD_FREQ=999 DUCKTAPE_DEBUNDLE_PROGRESS=1 \
  perf_wrapper.sh --output-dir <dir> -- \
  debundle_analyze --projection proj --tree-source-root . --out prof.analysis
```

Runs were on a 4-core VM with the `task-clock` software event (no hardware
counters). Phases were assigned from per-sample thread counts and the stage
function on each stack, cross-checked against the progress markers.

| Corpus             | Recipe                                                                                                                                                                                                                                                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Synthetic multi    | `gen_synth_corpus.generate_chunk` makes an 80,000-statement main chunk plus 400 leaf chunks of 300 statements. Each leaf imports 4 random main exports and 1 export of the previous leaf, and reads them eagerly. Seed 1; all chunks are `inline_in_entry`. |
| Synthetic single   | `gen_synth_corpus --statements 80000 --seed 1`.                                                                                                                                                                                                             |
| Public real bundle | The 65 `dist/prod` ESM chunks of `@excalidraw/excalidraw@0.18.0`, with dependencies installed (`npm install --ignore-scripts`) for `--packages-root`.                                                                                                       |

The multi-chunk recipe was a scratch extension and is not committed.

## Phase timelines

| Corpus           | Serial head                                             | Parallel middle     | Serial tail                                            |
| ---------------- | ------------------------------------------------------- | ------------------- | ------------------------------------------------------ |
| Synthetic multi  | 0–9.5 s: parse, program summary, main-chunk purity prep | 9.5–12 s, 3–4 cores | 12–19.5 s main-chunk facts; 19.5–23.4 s artifact write |
| Synthetic single | 0–4 s: parse 2.5 s, program summary 1.5 s               | none                | 4–12.5 s chunk facts; 12.5–14 s artifact write         |
| Real bundle      | 0–1 s                                                   | 1–3.5 s, 3–4 cores  | 3.5–18 s facts of one 440 KB chunk; 0.6 s write        |

The parallel middle is the per-chunk `par_iter` in
`staged_analysis::analyze_program`. Its input is the sorted chunk-id list, so
where the largest chunk runs is decided by its name:

- **Synthetic multi:** the main chunk sorts last. It started at 11.8 s, after
  every leaf had finished (11.5 s), and ran alone for 7.7 s.
- **Real bundle:** the expensive chunk sorts early. It started at 1.3 s, so its
  14 s overlapped the rest of the chunk work, and the tail is purely its own
  intra-chunk analysis.

In both corpora, the per-chunk analysis of one chunk is inherently serial:
statement-order interpretation and fixpoint inference within that chunk.

The synthetic corpus over-weights the head (41% of wall time, against 5% for
the private large web corpus). Its statements are cheap constant chains, so
per-statement interpreter cost is far below real code.

## Hot spots

Each row gives the share of samples within that tail window.

| Window                     | Symbol (inclusive unless noted)                                                                        |   Share | Class                                                                         |
| -------------------------- | ------------------------------------------------------------------------------------------------------ | ------: | ----------------------------------------------------------------------------- |
| Real-bundle tail           | `Executor::invoke_callback_pool_once`                                                                  |     73% | algorithmic: callback-pool fixpoint per opaque call                           |
| Real-bundle tail           | `Executor::callback_values` (reachability walk)                                                        |     54% | algorithmic: reachable heap × walks                                           |
| Real-bundle tail           | `Values::insert` under `callback_values` (`imbl` B-tree insert, `ArcK::make_mut`; ~34% self in `imbl`) |     36% | allocation/persistent-structure churn                                         |
| Real-bundle tail           | `export_surface_callback_values`                                                                       |     29% | algorithmic: whole export surface re-walked per pool iteration                |
| Real-bundle tail           | `invoke_possible_callbacks_transfer` → `callback_values`                                               |     21% | algorithmic: supplied-argument walk                                           |
| Synthetic main-chunk facts | `annotate_synchronous_calls`                                                                           |     59% | algorithmic: interpreter over all owners                                      |
| Synthetic main-chunk facts | `infer_synchronous_execution` (`IterativeInference<RuntimeRules>::solve`)                              |     31% | algorithmic: fixpoint                                                         |
| Synthetic main-chunk facts | `ExecutionIndex::collect`; `SourceUnitReadClosure::new`                                                | 13%; 9% | linear AST passes; set building                                               |
| Synthetic main-chunk facts | `malloc` + `cfree` + `drop_glue`                                                                       |     26% | allocation churn (`BTreeSet<RuntimeKey>` inserts, `ProofDependencies` clones) |
| Synthetic write            | `write_artifact`: `rmp_serde` into a single-threaded zstd encoder (zstd 44%); `snapshot` 19%           |     93% | serialization/compression                                                     |
| Synthetic exit             | dropping `BTreeMap<String, ChunkFactAnalysis>`                                                         |     73% | deallocation                                                                  |
| Synthetic single head      | `build_chunk_manifest_from_analysis` export-alias filter                                               |     87% | algorithmic: O(exports × declarations)                                        |
| Synthetic multi head       | `resolve_imported_callable_summaries` → `prepare_callable_capabilities`                                |     68% | serial loop; a past parallel version cost peak RSS                            |

The manifest filter calls `declarations.iter().any(|d| d.names.contains(local))`
once per export alias. It cost 2.1 s of the synthetic single run's 14 s, where
10% of statements are exported. On the real bundle it cost 0.13 s.

## Candidate optimizations, ranked

1. **Bulk-build the `callback_values` result.** The walk inserts each found
   callback into a persistent `imbl` set: 36% of the real-bundle tail is that
   insert path. Collecting into an ordinary set and converting once keeps the
   result identical. The upper bound is about 36% of that chunk's tail; the
   remaining `AbstractValue` comparisons stay. Evidence: the real-bundle profile
   above.
2. **Memoize `available_export_callbacks` / `export_callback_values` per world
   revision.** `possible_callback_pool` recomputes the export-surface walk on
   every fixpoint iteration of every opaque call: 29% of the real-bundle tail.
   This is not trivially safe: the key must cover every heap, prefix and
   publication mutation the walk observes.
3. **Start the largest chunk first.** Order the per-chunk `par_iter` inputs
   (and the cross-module purity map) by descending source size. Results are
   collected into a `BTreeMap`, so output order is unchanged. This saves up to
   the parallel-middle duration when the biggest chunk would otherwise start
   last: 2.6 s of 23.4 s on the synthetic multi corpus, nothing on the real
   bundle, and up to about 12 of 40 min on the private large web corpus.
   - It raises peak RSS, because the biggest chunk's facts overlap the others'.
   - Whether the private corpus benefits depends on when its biggest chunk
     started. Rerun that action once with
     `--action_env=DUCKTAPE_DEBUNDLE_PROGRESS=1`; the action passes the default
     shell environment through, and Bazel prints the stderr of successful
     actions.
4. **Parallelize the artifact write.** Per-chunk `snapshot` work can run under
   Rayon, and zstd has multi-threaded compression. This affects up to 15% of
   the synthetic wall time and 3% of the real bundle's. Multi-threaded zstd
   must keep its output byte-stable for a fixed worker count.
5. **Index declared names in `build_chunk_manifest_from_analysis`.** A hash set
   of declared names gives an identical result and is trivially safe. It is in
   the serial head, not the tail. It matters only for chunks with thousands of
   export aliases: 15% of the synthetic single run, under 1% of the real
   bundle.
6. **Skip the final deallocation of the analysis facts:** 4% of the synthetic
   multi run.

No candidate is both trivially safe and over 20% of a measured tail. Item 1
comes closest; its real effect needs a before/after profile on the same
inputs.

## Single-chunk shape

Large single chunks get no parallelism at all: the per-chunk `par_iter` has one
item. Their wall time is the head plus that chunk's facts plus the write. Items
1, 2 and 5 apply to them; item 3 does not.
