# `DebundleAnalyze` serial phases

Why the analysis action averages so few cores on large inputs, and what its
serial stretches cost on a public production bundle. Engine: public revision
`f4ccff2f`; the profiled code paths are unchanged at `e22674c9`.

## Remote executor allocation (private corpora)

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

## Public corpus: Excalidraw production package

Pinned inputs, staged outside Bazel:

| Package                  | Version | Integrity                                                                                         |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------- |
| `@excalidraw/excalidraw` | 0.18.0  | `sha512-QkIiS+5qdy8lmDWTKsuy0sK/fen/LRDtbhm2lc2xcFcqhv2/zdg95bYnl+wnwwXGHo7kEmP65BSiMHE7PJ3Zpw==` |
| `react`, `react-dom`     | 19.3.1  | `sha512-QNhWdt7n…`, `sha512-Y0bARz53…`                                                            |

```sh
npm install --no-audit --no-fund --ignore-scripts \
  @excalidraw/excalidraw@0.18.0 react@19.3.1 react-dom@19.3.1
```

That gives 228 packages, which serve as `--packages-root node_modules`. The
corpus is every `.js` file under
`node_modules/@excalidraw/excalidraw/dist/prod`: 65 esbuild ESM chunks,
including the entry, 4 shared chunks, 55 locale chunks and a worker. They are
listed sorted in `js-files.txt`.

The spec is a residual baseline:

- every chunk ID in `unassigned_mode` as `inline_in_entry`;
- empty `logical_modules` and `chunk_renames`;
- no vendor substitutions.

Commands:

```sh
debundle_project_analysis --spec spec.json --packages-root node_modules --out proj
DUCKTAPE_DEBUNDLE_PROGRESS=1 debundle_analyze --projection proj \
  --tree-source-root . --packages-root node_modules --out out.analysis
```

The binaries come from `bbr build -c opt
//devinfra/js/debundle:{debundle_analyze,debundle_project_analysis}`. Three
runs on a 4-core VM each took 17.1–17.8 s wall, 19.3–20.4 CPU s and 373–382 MiB
peak RSS.

### How it compares with the private shapes

| Measure           | Excalidraw                                                                                                                | Private                                                  |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Chunks            | 65                                                                                                                        | 807, 44, multi-chunk CLI, 2 single-chunk                 |
| Largest chunks    | entry: 730 top-level statements (502 KB); `chunk-FX7ZIABN`: 480 (439 KB); a 1.8 MB chunk with only 12 statements (0.15 s) | not in execution metadata                                |
| Phase proportions | 5% serial head, 13% parallel, 78% one chunk alone, 3% write                                                               | 807-chunk: 5% serial head, 30% parallel, 65% serial tail |

Statement counts are `acorn` `Program.body` lengths.

What this corpus does not represent:

- React and other dependencies are package imports, not vendored chunks.
- There is one dynamic-import worker.
- At 17 s, the whole run is about 100× shorter than the private web corpus.

### Phase timeline

Phases were attributed from `perf` samples, using thread counts per 0.5 s bin
and the stage function on each stack, and checked against the progress
markers.

| Window    | Cores | Stage                                                                                                                                                    |
| --------- | ----: | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0–1 s     |   1–2 | `collect_program_summary`: 74% `resolve_imported_callable_summaries`, of which 42% is serial `prepare_callable_capabilities` (`ExecutionIndex::collect`) |
| 1–3.5 s   |   3–4 | per-chunk `par_iter` in `staged_analysis::analyze_program`                                                                                               |
| 3.5–18 s  |     1 | `chunk-FX7ZIABN` `analyze_logical_chunk`, alone                                                                                                          |
| 18–18.6 s |     1 | `write_artifact` (`rmp_serde` into single-threaded zstd)                                                                                                 |

The `par_iter` input is the sorted chunk-id list. `chunk-FX7ZIABN` sorts early
and started at 1.3 s in all three runs, so its 16.3 s overlapped the other
chunks. The tail is purely that chunk's own analysis: statement-order
interpretation and fixpoint inference within one chunk, which is inherently
serial at this granularity. The entry chunk, with more statements, took 1.8 s.

## Hot spots in the tail

The profile used `perf_wrapper.sh` with `PERF_CALL_GRAPH=fp
PERF_RECORD_FREQ=999` on a second analyzer build with
`--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes`.
DWARF unwinding of the stock binary recovered multi-frame stacks for only 29%
of samples. The event is `task-clock`; the VM has no hardware counters.

| Symbol (inclusive unless noted)                                                                         | Share of tail | Class                                                          |
| ------------------------------------------------------------------------------------------------------- | ------------: | -------------------------------------------------------------- |
| `Executor::invoke_callback_pool_once`                                                                   |           73% | algorithmic: callback-pool fixpoint per opaque call            |
| `Executor::callback_values` (reachability walk; 12% self)                                               |           54% | algorithmic: reachable heap × walks                            |
| `Values::insert` inside `callback_values` (`imbl` B-tree insert, `ArcK::make_mut`; ~34% self in `imbl`) |           36% | allocation/persistent-structure churn                          |
| `export_surface_callback_values`                                                                        |           29% | algorithmic: whole export surface re-walked per pool iteration |
| `invoke_possible_callbacks_transfer` → `callback_values`                                                |           21% | algorithmic: supplied-argument walk                            |
| `bounded_invocation_fallback_transfer` → `callback_values`                                              |           15% | algorithmic                                                    |
| `drop_glue` (self)                                                                                      |            9% | deallocation churn                                             |
| `AbstractValue::cmp` (self)                                                                             |            6% | ordering inside the persistent sets                            |

The hot-spot table above is the profile before the transient walk set landed.
`possible_callback_pool` recomputes `available_export_callbacks()` on every
iteration of each opaque call's callback fixpoint.

## Landed: transient `callback_values` walk set

`walk_callback_values` used its persistent `imbl` result as its visited set.
It now probes an `FxHashSet` and builds the ordered `Values` once at the end.

Before/after on the same staged inputs and spec, with the base and candidate
built from the same tree except for this change:

| Measure                             | Before      | After        |
| ----------------------------------- | ----------- | ------------ |
| Tail samples (999 Hz `task-clock`)  | 15,424      | 9,971 (−35%) |
| `callback_values` inclusive samples | 8,437       | 3,619 (−57%) |
| `Values::insert` share of tail      | 39.2%       | 1.7%         |
| Wall, 3 runs without `perf`         | 18.2–18.6 s | 13.6–13.9 s  |
| CPU, same runs                      | 20.2–20.6 s | 15.6–16.0 s  |
| Peak RSS, same runs                 | 376–377 MiB | 377–378 MiB  |

Output identity:

- The `.analysis` bodies are byte-identical. The headers are identical once
  `analysis_engine_sha256` is masked.
- A full `debundle run --graphs` (789 files: emitted modules, dependency
  graphs, gate and translation-validation reports) is byte-identical, run from
  the same binary path, spec and output directory. The only difference is
  `engine_sha256` in `program_graph.json.gz`.

Within `callback_values`, `AbstractValue` ordering compares ids:
`Captured` compares by interned identity, and no `Atom` strings are compared
there.

## Candidate optimizations, ranked

1. **Memoize the export-surface callback walk.** It is 25% of the current tail.
   Not trivially safe: the key must cover every heap, prefix and publication
   mutation the walk observes.
2. **Start the largest chunk first.** Order the per-chunk `par_iter` inputs by
   descending source size. Results are collected into a `BTreeMap`, so output
   order is unchanged. It does nothing here, because the expensive chunk
   already starts first. On a corpus whose biggest chunk sorts last it saves up
   to the length of the parallel phase: up to about 12 of 40 min on the private
   large web corpus. It raises peak RSS.
   - Whether the private corpus benefits depends on when its biggest chunk
     started. Rerun that action once with
     `--action_env=DUCKTAPE_DEBUNDLE_PROGRESS=1`; the action passes the default
     shell environment through, and Bazel prints the stderr of successful
     actions.
3. **Parallelize `prepare_callable_capabilities` in the head.** It is 42% of a
   5% head. A past parallel version cost peak RSS.
4. **Parallelize the artifact write.** Per-chunk `snapshot` work can run under
   Rayon, and zstd has multi-threaded compression. It is 3% here. Multi-threaded
   zstd must keep its output byte-stable for a fixed worker count.

Single large chunks get no parallelism at all, because the per-chunk `par_iter`
has one item. Item 1 applies to them; item 2 does not.
