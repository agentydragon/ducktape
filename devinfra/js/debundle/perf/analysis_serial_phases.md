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

## Landed: export-surface callback memo

`possible_callback_pool` used to recompute the export-surface callback walk on
every fixpoint iteration of every opaque call. `ExecutionState` now keeps the
last walk per surface, together with clones of each persistent world
collection the walk reads and the `Values` of each lexical cell it read
through closure captures. It reuses the result only when every one of those
is still `ptr_eq` to the current world. Holding a clone makes any later write
copy the collection, so pointer equality proves the contents are equal.

Back-to-back on the same inputs, after the transient walk set:

| Measure                                    | Before        | After         |
| ------------------------------------------ | ------------- | ------------- |
| Total samples (999 Hz `task-clock`)        | 12,290        | 11,358 (−8%)  |
| Tail samples                               | 7,485         | 6,978 (−7%)   |
| `export_surface_callback_values` inclusive | 1,972 (26.3%) | 1,050 (15.0%) |
| Wall, 3 interleaved runs without `perf`    | 10.7–11.1 s   | 9.7–10.1 s    |
| CPU, same runs                             | 12.3–12.8 s   | 11.3–11.6 s   |
| Peak RSS, same runs                        | 377 MiB       | 377 MiB       |

In the same interleaved batch, the engine before both changes took 14.8–15.8 s
wall and 16.3–16.7 CPU s. The VM was faster in this batch than in the earlier
transient-set measurement, so compare figures within one batch only.

The remaining 15% is walks after a world change, where the memo misses. Output
identity: the `.analysis` bodies are byte-identical, and so are all 789 files
of `debundle run --graphs`, apart from `engine_sha256`.

## App-build corpus (`//devinfra/js/debundle/excalidraw`)

The production app build of the same package, with React and vendor code
bundled, run with its residual-baseline tree spec.

Inputs were staged with `bbr build //devinfra/js/debundle/excalidraw:{source,inputs}`.
The projection comes from `debundle_project_analysis --tree-config ...
--out-root ...`. The current spec's vendor marks need `--package-root` with
`clsx@1.1.1` and `roughjs@4.6.4`, because a copied analyzer has no runfiles.

Two bundle shapes:

- **Split:** current `devel`, where `app/vendor/` puts roughjs and clsx in
  their own chunks.
- **Inlined:** #15267's commit `6114b352`, where roughjs sits inside the
  545 KB shared editor chunk.

`-c opt` analyzers on a 4-core VM, one run each:

| Engine                           | Split: wall, CPU, RSS   | Inlined: wall, CPU, RSS   |
| -------------------------------- | ----------------------- | ------------------------- |
| Before both changes (`e22674c9`) | 36.0 s, 51.2 s, 642 MiB | 397.4 s, 425.8 s, 600 MiB |
| Transient walk set               | 35.0 s, 49.9 s, 581 MiB | 385.1 s, 412.8 s, 597 MiB |
| Plus export-surface memo         | 34.6 s, 48.3 s, 582 MiB | 361.9 s, 390.4 s, 598 MiB |

The `.analysis` bodies of all three engines are byte-identical on both shapes.

Where the time goes:

- **Split:** `main` takes about 30 s of chunk analysis, and the editor chunk
  takes 14–16 s alongside it.
- **Inlined:** the editor chunk takes 358–394 s, alone after the first 2.3 s.
  It is single-threaded, and its cost is about 25× the split editor chunk's for
  72 more source units out of 1,859.

### Inlined editor chunk profile

80,276 samples at 199 Hz, `task-clock`, frame pointers, with the export-surface
memo engine:

| Symbol (inclusive unless noted)                                                     |       Share | Class                                                              |
| ----------------------------------------------------------------------------------- | ----------: | ------------------------------------------------------------------ |
| `runtime_callbacks::dispatch` → `invoke_possible_callbacks_transfer`                |         59% | algorithmic: callback-pool fixpoint per opaque call                |
| `promote_deferred_callbacks`                                                        |         40% | algorithmic                                                        |
| `bounded_invocation_fallback`                                                       |         35% | algorithmic                                                        |
| building an `imbl` `OrdSet<AbstractValue>` from an iterator, one insert per element |       32.5% | persistent-structure churn, spread over callers (each 2–7%)        |
| `Executor::unsettle`                                                                |       26.5% | algorithmic: reachable heap × fallback events                      |
| `OrdMap<(Atom, SyntaxContext), Values>` operations                                  |       22.7% | name-keyed lookups; 13 points of it under `unsettle`               |
| `callback_values`; `export_surface_callback_values`                                 | 15.9%; 9.5% | algorithmic (remaining misses)                                     |
| `Atom::cmp` + `hstr::Atom::as_str` (self)                                           |       12.0% | string ordering of binding names; `unsettle` is the largest caller |

`unsettle` runs on each bounded fallback and each unknown call. Each run
re-walks every value reachable from its inputs. For every reachable callable
it looks up each of the callable's read bindings in `prefix_values`, which is
keyed by `(Atom, SyntaxContext)`, so every lookup compares binding names as
strings. The private single-chunk flat profile has the same shape: `imbl`
inserts, `Atom` ordering, `AbstractValue` ordering, `callback_values` and
`unsettle` as the largest self cost.

Not yet measured:

- which roughjs construct makes the reachable graph or the fallback count grow;
- how cost scales when that chunk grows or shrinks.

Both need bundles rebuilt at intermediate sizes.

## Landed: prefix values keyed by binding ordinal

`World.module.prefix_values` was an `imbl` map keyed by `(Atom,
SyntaxContext)`, so each lookup ordered binding names as strings.
`PrefixValues` now owns the same map, keyed by each binding's ordinal in a
per-run `TopBindings` table. Ordinals follow `Id` order, so iteration and joins
are unchanged, and a lookup hashes the `Id` once and then compares integers.

Base is `devel` at `d2f5b0f4`. Runs are single, on the same 4-core VM.

| Shape   | Before                    | After                     |
| ------- | ------------------------- | ------------------------- |
| Split   | 34.5 s wall, 50.2 s CPU   | 24.3 s wall, 37.0 s CPU   |
| Inlined | 347.5 s wall, 375.2 s CPU | 299.1 s wall, 318.7 s CPU |

Peak RSS is unchanged (643 and 593–600 MiB). The `.analysis` bodies are
byte-identical on both shapes.

On the inlined editor chunk, against the profile above (export-surface memo
engine), total samples went from 80,276 to 62,428:

| Measure                                        | Before       | After |
| ---------------------------------------------- | ------------ | ----- |
| `Atom::cmp` self                               | 7.6%         | 2.5%  |
| `unsettle` inclusive                           | 26.5%        | 18.3% |
| `PrefixValues::get` inclusive                  | (name-keyed) | 9.1%  |
| `invoke_possible_callbacks_transfer` inclusive | 59.4%        | 59.9% |
| `OrdSet` construction inclusive                | 32.5%        | 33.9% |

To rebuild the inlined shape, check out the corpus before roughjs was split:

```sh
git worktree add --detach <dir> 6114b352
cd <dir> && bbr build //devinfra/js/debundle/excalidraw:{source,inputs}
```

Then project and analyze its `bb-out` tree as above, with no `--package-root`;
its vendor marks are empty.

## Landed: transient callback inputs

The callback walks (`callback_values`, `unsettle`, `taint_objects`,
`expose_class_capabilities`) only iterate their input. Before, callers built
that input as a persistent `Values`, one insert per element. Now the walks
take any iterator of values:

- **Transient inputs** are a sorted, deduplicated `Vec` (`ordered_values`). It
  holds the same elements in the same order as the `Values` it replaces.
- **Stored sets** (the global escaped callbacks and the callback pool) insert
  only absent values. A present value no longer copies the persistent path.

Base is `devel` at `a6199437`. Runs are single, on the same 4-core VM.

| Shape   | Before                    | After                     |
| ------- | ------------------------- | ------------------------- |
| Split   | 23.9 s wall, 36.9 s CPU   | 23.3 s wall, 34.7 s CPU   |
| Inlined | 289.8 s wall, 308.8 s CPU | 268.2 s wall, 287.2 s CPU |

Peak RSS is unchanged. The `.analysis` bodies are byte-identical on both
shapes.

Inlined editor chunk profile, before and after:

| Row                                  | Before |  After |
| ------------------------------------ | -----: | -----: |
| Total samples                        | 62,428 | 56,806 |
| `OrdSet` construction (inclusive)    |  33.9% |  14.2% |
| `ordered_values` sort (inclusive)    |      — |   9.9% |
| `invoke_possible_callbacks_transfer` |  59.9% |  57.1% |
| `promote_deferred_callbacks`         |  42.2% |  41.6% |

## `unsettle` call pattern (inlined editor chunk)

Measured with temporary, uncommitted instrumentation on the binding-ordinal
engine:

| Measure                                                     | Value                         |
| ----------------------------------------------------------- | ----------------------------- |
| Calls                                                       | 396,047                       |
| Values visited                                              | 93.4 M                        |
| Visited per call: mean, median, p90, max                    | 236, 327, 481, 1,735          |
| Input values per call: median, max                          | 254, 789                      |
| Calls that inserted no new unsettled object                 | 99.8%                         |
| Inputs equal to the previous call's                         | 29.5% of calls, 40.8% of work |
| Inputs a subset of the previous call's                      | 33.7% of calls, 42.4% of work |
| Reads unchanged since the previous call                     | 61.3% of calls, 40.5% of work |
| Both subset inputs and unchanged reads (memo hits, at most) | 16.6% of calls, 10.4% of work |

"Reads" are the persistent collections `prefix_values`,
`prefix_object_properties`, `applied_accessors` and `descriptors`, compared
with `ptr_eq`.

Every effect `unsettle` has is idempotent:

- cell `Top` and TDZ widening;
- an `unsettled_objects` insert;
- `writable_prefix` → `Unknown`;
- a revoked-result insert.

So a previous-call memo would be sound, but its ceiling is about 10% of
`unsettle`, under 2% of the run. It was not built.

The walk almost always re-derives state that already holds. That points to an
incremental frontier instead:

- **Retain the expanded set.** Keep the set of values already expanded, with
  their effects in place, across calls.
- **Revalidate per element, against that element's own reads:**
  - a closure: its captured cells' `Values`, and its callable's `prefix_values`
    reads;
  - an object: its `prefix_object_properties` and `applied_accessors` entries;
  - a descriptor map: its `descriptors` entry.
- **Skip only what still validates.** A call expands only values outside the
  frontier, or whose reads changed.

Validation is per element, not one `ptr_eq` on a whole collection, so this is
a design change rather than a memo.

## Cost model (inlined editor chunk)

Measured with temporary, uncommitted per-owner counters on the transient-input
engine. A run executes 4,297 owners in the editor chunk.

**Serial share.** The editor chunk runs alone from 2.3 s to the end of a
268 s run, so about 99% of wall time is in one chunk's single-threaded
analysis. In the split shape, the editor chunk overlaps `main`.

**Where it goes.** 162 owners take 93% of the chunk's 276 s.

- They form one contiguous run of source-unit ordinals, 754–932.
- Nearly all are icon constants of the form
  `X = k((0, f.jsxs)("g", {children: [(0, f.jsx)("path", …), …]}))`.
- `f = ca(u1())` is esbuild's CommonJS interop wrapper (imported from another
  chunk) around the bundled JSX runtime.
- So every `f.jsx`/`f.jsxs` call is a call on an opaque value, and each one
  runs the possible-callback pool.

Per-owner means, bucketed by the size of the escaped-callback pool when the
owner runs:

| Escaped callbacks | Owners | Mean time | Opaque-call pool passes | Mean pool size | Deferred promotions |
| ----------------: | -----: | --------: | ----------------------: | -------------: | ------------------: |
|              0–49 |  1,952 |    6.7 ms |                      35 |             28 |                  14 |
|           300–349 |    186 |    777 ms |                     393 |            337 |                 107 |
|           350–399 |    556 |    201 ms |                      94 |            354 |                  25 |
|           400–449 |    218 |    5.2 ms |                     1.6 |            436 |                 0.6 |

A heavy icon owner does about 850 pool passes over about 354 callbacks, with
about 870 bounded fallbacks, about 1,450 `unsettle` calls, and 228 deferred
promotions.

**Scaling.** Passes per owner grow with the pool: roughly 1.2 passes per
pooled callback, from 28 callbacks to 337. Each pass costs time proportional
to the pool, a median of about 6 µs per pass·callback. So:

> cost per opaque-calling owner ∝ pool², and chunk cost ∝ (owners making
> opaque calls while the pool is large) × pool².

Between those two buckets, mean owner time grows 116× (6.7 ms to 777 ms)
while pool² grows 145×.

Inlining roughjs matters because its classes and closures escape into this
chunk's pool before the icons run. Splitting it out keeps the pool small at
that point: the split editor chunk takes 14–16 s, against 260–390 s inlined.

## Candidate optimizations, ranked

From the cost model above:

1. **Memoize the reentrant-pool fallback.** An opaque call inside an active
   pool member takes a bounded fallback over the whole pool. A heavy owner
   repeats about 870 near-identical fallbacks, and each re-walks the pool's
   reach. Memoizing them removes one factor of the pool from the per-owner
   cost.
2. **Make callback promotion a per-owner worklist,** so each deferred callback
   is promoted once per state change, not once per opaque call.
3. **Make `unsettle` incremental** with the per-element frontier above. It is
   now 7% inclusive, so this comes after 1 and 2.

Earlier candidates, from the package build:

3. **Start the largest chunk first.** Order the per-chunk `par_iter` inputs by
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
4. **Parallelize `prepare_callable_capabilities` in the head.** It is 42% of a
   5% head. A past parallel version cost peak RSS.
5. **Parallelize the artifact write.** Per-chunk `snapshot` work can run under
   Rayon, and zstd has multi-threaded compression. It is 3% here. Multi-threaded
   zstd must keep its output byte-stable for a fixed worker count.

Single large chunks get no parallelism at all, because the per-chunk `par_iter`
has one item. Item 3 does not help them.
