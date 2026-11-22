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

## Interop wrapper: not the cause

Scratch builds of the inlined proxy with the editor chunk's source hand-edited,
measured with the per-owner counters. Heavy owners are the 160 units in
754–932 that take at least 0.2 s each (256 s of 276 s).

| Variant                                                             | Remaining refusals                                                                                                                                                  | Heavy owners still pooling | Owner time |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------: | ---------: |
| Original `var f=ca(u1())`                                           | `unknown_call_target` at `ca(` and `u1(` (the imported `__toESM`, `__commonJS`); in each icon, `member_read` at every `f.jsx` and `unknown_call_target` at the call |                        160 |      276 s |
| v1: factory body inlined, `var f=(()=>{var Cr={}; …; return Cr})()` | `member_read` and `unknown_call_target` at both `Symbol.for(…)`; icons unchanged                                                                                    |                        160 |      282 s |
| v2: v1 with `Symbol.for(…)` replaced by string constants            | `property_write` at `Cr.jsx=c1`; `member_read` at every `f.jsx`                                                                                                     |                        160 |      279 s |
| v3: all 734 `(0,f.jsx[s])(` calls go to one top-level function      | inside the function: `implicit_coercion`, `member_read` (`t.key`, `a.ref`), `membership_protocol` (`"key" in t`), `property_write` (`a[i]=t[i]`)                    |                        160 |      589 s |

- v2's `property_write` is `promote_property_writes`: once
  `reachability.unknown_call_seen` is set, which is sticky, every later write
  takes the possible-setter path, even to a fresh `{}`.
- v2's `f.jsx` reads take `promote_possible_getter`. The components in the
  pool capture `f`, so every bounded pool pass unsettles `f`'s object.
- In v3 the body now runs, and under the fallback the escaped components run
  too (clsx and roughjs subclasses), so the time doubles.

So the pools do not come from the wrapper. A statement enters a fallback the
size of the whole pool on any single refusal while the escaped pool exceeds
`MAX_SYNCHRONOUS_CALLBACK_POOL`: 200 callbacks by unit 404, 338 by 754. After
the chunk's first opaque call, refusals are everywhere.

## Delta ceilings (inlined editor chunk)

Unsound upper bounds, in scratch builds only. Within a top owner, each level
skips the named work when every input value was already processed in that
owner. That over-skips, because it ignores writes that narrow state, so each
row bounds what any sound monotone delta of the same scope can save. Times
are owner-time sums from the counter build.

| Skipped when no input is new                                                        | Owner time | Heavy owner |
| ----------------------------------------------------------------------------------- | ---------: | ----------: |
| Nothing (baseline)                                                                  |      276 s |       1.7 s |
| Fallback callback walk and `unsettle`, and the pool tail (taint, walk, escape)      |      205 s |      1.37 s |
| Also `promote_deferred_callbacks`, `promote_raw_callbacks`, `promote_class_members` |       92 s |       0.6 s |
| Also a whole `invoke_possible_callbacks_transfer`                                   |       60 s |       0.4 s |

- After the first level, the profile shows `promote_class_members` at 28%,
  `promote_lazy_effects` at 16%, `export_surface_callback_values` at 12%,
  `note_unknown_execution` at 10%, and the fallback at 17%. The
  export-surface memo misses because each pool invocation changes the world.
- After the last level, what remains is `promote_possible_getter` at 32%,
  mostly its `unsettle` of the prefix values of every binding the statement
  has read (27%). Then come `note_unknown_execution` at 15% and
  `promote_lazy_effects` at 15%. The trigger is the `f.jsx` read on an object
  that every pool pass unsettles.

**Conclusion.** A sound delta on this engine is bounded at about 3–5× on the
proxy (60–92 s against a 20 s target). Reaching the target needs fewer
opaque events: what a bounded pool pass unsettles, and the sticky
possible-setter path. Narrowing either turns refusals into acceptances, so it
takes the output-changing review. Plan:
<../plans/analysis_performance.md#callback-analysis-cost-and-budgets>.

With `program_premises.intrinsics_never_written: {families: [object]}` added
to the original and v2 configs (`all` is refuted by census writes on `Error`
and `Array`), all 160 heavy owners still pool (287 s and 289 s). The
possible-setter and possible-getter paths do not consult the premise today.

## Candidate optimizations, ranked

From the cost model above:

1. **One per-owner frontier over every opaque-call path:** the pool and its
   fallback, callback promotion, the export-surface walk, and the
   possible-getter `unsettle`. The ceiling is 4.6× (§ Delta ceilings); scoped
   to the fallback alone it is 1.35×.
2. **Fewer opaque events.** Even with the full delta, 60 s remains above the
   20 s target. The events come from what a pool pass unsettles and from the
   sticky possible-setter path (§ Interop wrapper: not the cause). Narrowing
   either changes output, so it needs the output-changing review.

Making the interop wrapper analyzable gains nothing alone (§ Interop wrapper:
not the cause), but item 2 needs it to reach the icons.

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

## Shared proof propagation (2026-11-17)

Matched optimized runs compare `6791c1b6bd` with the production code in
`4b28321584`. Both include the compiled capability-containment pass; this is
not a measurement of #17107's impact. The frozen expanded Excalidraw source/spec
comes from the `4faeaf1dd2` checkpoint, with unchanged clsx/roughjs package roots.
The verified input-manifest SHA-256 is
`87f384df3d6ac9032494e2f81ade2a972bb04285eb5bbaea1d5e5ff6a28a2b9d`.

The host was wyrm2, Linux 6.18.55, 32 logical CPUs, default Rayon width. Three
runs per engine alternated baseline/candidate/candidate/baseline/baseline/candidate.
Each projected, analyzed and emitted afresh. Native `perf stat` and GNU `time`
measure wall time, CPU and process peak RSS; build/staging and RBE queue time
are excluded. With only three samples, nearest-rank p90 is the maximum.

| Metric                         | Baseline p50 / p90 / mean   | Candidate p50 / p90 / mean  |
| ------------------------------ | --------------------------- | --------------------------- |
| Whole pipeline wall, seconds   | 16.32 / 16.52 / 15.97       | 15.29 / 15.63 / 15.32       |
| Analysis wall, seconds         | 11.19 / 11.37 / 10.94       | 10.46 / 10.67 / 10.49       |
| Analysis CPU, seconds          | 18.83 / 18.86 / 18.36       | 17.72 / 17.94 / 17.78       |
| Analysis peak RSS, MiB         | 927.89 / 929.87 / 928.06    | 934.13 / 934.17 / 900.98    |
| Analysis instructions, billion | 156.310 / 156.319 / 156.312 | 157.134 / 157.149 / 157.138 |

Whole-pipeline wall ranges overlap: 15.07–16.52 s versus 15.03–15.63 s.
Candidate RSS ranges from 834.64 to 934.17 MiB. These samples establish neither
an aggregate speedup nor a stable RSS difference. Analysis instruction overhead
is about 0.53%. All six runs passed the normal gate and emitted identical sets
of 156 JavaScript files, including their bytes.

The first implementation used about 5% extra analysis instructions. It retained
proof edges after completed request/result payloads were consumed and copied
empty dependency sets. Retiring those edges at `take_final` and skipping empty
joins reduced that overhead. RSS also varied substantially in the first
cohort, so its roughly 100 MiB median difference cannot be assigned wholly to
the index. The maintained algorithm and lifetime bounds are in
[inference scheduling](../docs/inference.md#scheduling); a deterministic test
checks that 256 completed transfers leave no live proof edges after each step.
The old-engine work-count control needs three rule evaluations where the new
engine needs two, while retaining the same typed dependency.

Optimized binary build receipts:
[baseline](https://app.buildbuddy.io/invocation/d39ccbdf-8ff5-4c67-8cc6-6dd76f8cf257)
and [candidate](https://app.buildbuddy.io/invocation/f714541a-71bf-4847-9ee5-5cd0b1a9f6e7).
The candidate build applied the working patch to `31d9d65b48`; its production
Rust sources match `4b28321584`. The
[previous-engine negative control](https://app.buildbuddy.io/invocation/b2e74f49-1d19-49bd-a349-43d7e3bb59f2)
fails on the extra evaluation; the
[analysis/Clippy and CLI checks](https://app.buildbuddy.io/invocation/7d5c312d-4b64-400c-9cd1-bda8e63e5d92)
pass with edge retirement. Tana was not rerun, and full invocation-origin delta
propagation remains PF1 work.

## Integrated devel checkpoint (2026-11-17)

Optimized binaries at `c943a196de` combine devel `eca9b935d6` with #17140;
their Debundle subtree matches devel `b37be1ecdc`, including #17136 and #17140.
PF5 receiver confinement has no production implementation in this checkpoint.
The host is wyrm2, Linux 6.18.55, 32 logical CPUs, default Rayon width. Inputs
are the frozen expanded Excalidraw corpus above and Tana Web snapshot
`78d928dca7`'s passing ownership subset, with full-program projection/analysis.
Source/spec/package hashes were checked before and after execution. Neither
committed spec tree has changed since capture.

Each corpus had a 600 s workload budget, shared between the timed pipeline and
heap tracing. Builds, staging, hash verification and offline report generation
are excluded. GNU `time` measures wall/CPU/peak RSS; `perf` samples user CPU at
49 Hz with DWARF stacks. A separate unsampled Excalidraw confirmation remains
inside its budget. These are observations on a busy shared host, not a matched
before/after comparison or a speedup claim.

| Workload/run            | Pipeline wall | Pipeline CPU |   Peak RSS | Result                  |
| ----------------------- | ------------: | -----------: | ---------: | ----------------------- |
| Excalidraw, CPU sampled |       18.48 s |      28.22 s |  0.937 GiB | Complete                |
| Excalidraw, unsampled   |       31.87 s |      29.54 s |  0.943 GiB | Complete                |
| Tana, CPU sampled       |      508.11 s |     506.24 s | 16.320 GiB | Stopped during analysis |

Tana spent 90.99 s projecting and 417.12 s analyzing; it never reached Apply.
Its separate heap replay consumed 90.53 s. Total timed workload execution was
598.64 s; profiler finalization added about 6 s outside the workload. Tana's
completion time and eventual peak memory are unknown. Excalidraw's two runs
and the prior checkpoint emit the same 156 JavaScript files byte for byte.
The large wall variation is not reproduced in CPU time; do not infer a stable
wall-time distribution or regression from these two runs.

Tana analysis self samples put about 54% in `FlowKey` fact-table lookup/update
and dependency membership/index operations, and 19% in Roaring subset checks.
Its trace reports 1,153 lost samples, so percentages are approximate.
Excalidraw analysis is spread across atom/value comparisons, persistent-map
lookup, `bounded_invocation_fallback_transfer`, `walk_callback_values` and
`promote_top_effects`; no individual self symbol exceeds about 4.1%.

Heaptrack's complete Excalidraw analysis records a 380.0 MB live-heap peak.
Grouping allocations at that peak by their recorded owning stack gives 42.7%
ordered effects/interpreter state, 27.1% source value-flow facts/dependency
indexes and 15.2% other cross-module purity preparation. RSS includes allocator
retention and other process memory; these percentages do not partition RSS.

Tana's heap replay was stopped after 90 s and reaches a 4.615 GB tracked-heap
peak. At that captured peak, source value-flow facts/indexes own 63.1% and
ordered effects/interpreter state 21.2%. Set-insertion paths within `solve_flow`
account for 1.585 GB, including watcher and watched-key membership. Other large
recorded paths include the watcher map, property-fact table and copy-on-write
bitmap container clones.
This early allocation profile does not attribute the later 16.320 GiB native
RSS peak. Unfreed allocations at forced termination are not evidence of leaks.

## Class provenance, property reuse and dependency storage (2026-11-17)

This matched pair uses the same frozen inputs and host as the integrated
checkpoint. Baseline tools at `c943a196de` have the same production Debundle,
dependency and Bazel configuration as devel `d5707a0464`; the only Debundle
differences are three E2E test files. Candidate integration `6fe546e91d` adds
the bounded class-provenance closure, property receiver/expression reuse, and
compact reverse dependency index. Its complete tree matches the pre-rebase top
of native stack #17194 at `a2d1b1bd49`: [#17181](https://github.com/agentydragon/ducktape/pull/17181)
→ [#17180](https://github.com/agentydragon/ducktape/pull/17180)
→ [#17179](https://github.com/agentydragon/ducktape/pull/17179).
The stack subsequently rebased onto `a356171ea0`; its `facts/` and `inference/`
trees remain identical to the measured candidate, but upstream emission and CLI
refactors mean these are not exact-head measurements of the rebased pipeline.

The optimized tools were downloaded by advertised CAS digest from the
[combined build](https://app.buildbuddy.io/invocation/b2ab82e7-70a3-4d10-878d-8ecfde580f64).
That invocation explicitly checks `analysis`, passes 431 routine analysis tests,
all five requested CLI targets and the three separately selected manual work
sweeps. The candidate contains the correction preserving shared full-current RHS
bitmaps; it does not use the rejected per-receiver RHS-delta fanout.

GNU `time` and user-CPU `perf` sampling at 49 Hz with DWARF stacks use the same
commands for each engine. Each corpus/engine pair gets a fresh 600 s pipeline
budget; there is no heap replay in this pair. Builds, shell preparation, input
verification and offline reports are outside the budget. Hashes were checked
before and after each run. The Excalidraw input-manifest SHA-256 remains
`87f384df3d6ac9032494e2f81ade2a972bb04285eb5bbaea1d5e5ff6a28a2b9d`;
Tana's is `63e61ca7c8da2127711c8b3de2a5ace191fa5f35b90a63f72dfe18d953d6f814`.
Tana projects/analyzes the full captured program and applies only its passing
ownership subset. These are single pairs on a busy shared host, not a timing
distribution or measurements of each PR's separate contribution.

| Corpus / engine      | Pipeline wall | Pipeline CPU |   Peak RSS | Result             |
| -------------------- | ------------: | -----------: | ---------: | ------------------ |
| Excalidraw baseline  |       15.79 s |      24.64 s |  0.846 GiB | Complete           |
| Excalidraw candidate |       16.31 s |      25.00 s |  0.948 GiB | Complete           |
| Tana baseline        |      598.91 s |     592.16 s | 15.047 GiB | Capped in analysis |
| Tana candidate       |      599.06 s |     597.83 s | 16.519 GiB | Capped in analysis |

Excalidraw analysis alone takes 10.84 s wall / 18.38 s CPU on baseline and
11.14 s / 18.56 s on candidate. Its observed peak RSS is higher on candidate;
this pair establishes no end-to-end speedup. Both emit the same 156 JavaScript
files byte for byte. The Tana baseline spends 73.86 s projecting and 525.05 s
analyzing, without reaching Apply. Its profiler shutdown adds about four seconds
outside the native workload. Completion time and eventual peak memory remain
unknown. Candidate projection takes 79.14 s and analysis 519.92 s before its
cap, also without reaching Apply; profiler finalization adds about six seconds.
Its observed RSS is about 1.47 GiB higher. The incomplete runs do not establish
relative throughput or a stable peak-memory regression: the engines may have
reached different propagation progress at cutoff. Isolate the contributing
slice and retained state before assigning the increase to a particular cache or
index. Neither corpus pair establishes an aggregate runtime improvement.

Candidate Tana self samples remain concentrated in Roaring store subset checks
(18.4%), inference reads (15.6%), `FlowKey` equality (12.1%), dirty-key B-tree
insertion (11.0%) and proposals (6.6%). Inlining changes the symbol boundaries;
the candidate trace also reports 36.14% sample loss, so these approximate shares
cannot quantify saved operations or reliably compare symbol percentages with
baseline. Excalidraw remains
spread across atom/value comparisons, allocation and callback interpretation.

The controls establish narrower work and storage properties, not a complete
incremental engine or a whole-program polynomial bound. Namespace equality and
publication recursion remain; changed property expressions and full-RHS joins
still scan sets; forward key membership and historical subscriptions remain.
Future delta designs must account for unique bitmap payloads and copy-on-write
expansion as well as logical deliveries. The property-sharing witness caught
five retained payloads where the original transfer retained one, before the
candidate was corrected. Test-only counters are not production corpus counts.

## Initializer target index: optimized Excalidraw replay (2026-11-16)

This matched replay compares optimized `devel` tools at `dc4d9e5d26` with the
initializer-target index change in this PR. Both engines used the same
6.0 MiB canonical Excalidraw corpus at
`/tmp/agents/2026_11_16/debundle-performance-checkpoint/devel-4fae-corpus`,
including identical spec, source tree and package roots. The three-stage argv
was taken from the integrated Excalidraw profile; each engine received its own
projection, analysis and output directories for every run.

Runs used `RAYON_NUM_THREADS=4`, a 16 GiB address-space limit, a 600 s per-stage
timeout and GNU `time -v` without a profiler. Three engine pairs were
interleaved baseline/candidate, candidate/baseline, baseline/candidate. The
table reports medians; pipeline wall and CPU sum the three sequential stages.
Peak RSS is the median of each run's largest stage peak.

| Stage / pipeline | Baseline wall | Candidate wall | Baseline CPU | Candidate CPU | Baseline peak RSS | Candidate peak RSS |
| ---------------- | ------------: | -------------: | -----------: | ------------: | ----------------: | -----------------: |
| Project          |        0.49 s |         0.53 s |       0.80 s |        0.85 s |       301,556 KiB |        305,100 KiB |
| Analyze          |       11.19 s |        10.62 s |      17.45 s |       17.19 s |       721,608 KiB |        720,472 KiB |
| Apply            |        5.13 s |         4.52 s |       4.91 s |        5.10 s |       595,844 KiB |        659,512 KiB |
| Pipeline         |       16.81 s |        15.84 s |      23.29 s |       23.09 s |       721,608 KiB |        720,472 KiB |

Candidate median pipeline wall is 5.8% lower and CPU is 0.9% lower in this
workload; this passes the 10% slowdown bound but is not evidence of a stable
speedup or a Tana result. One baseline project run took 3.41 s versus 0.48 and
0.49 s in its other cycles; the table uses the median. Candidate analysis
reached 816,308 KiB in one cycle, although its median phase peak is lower.
Projection, analysis and emitted-output hashes matched in all three pairs:
`9d424dac670d1548676d400169af9986e2293589d9fd188e78cfea3278e449a1`,
`d7ceb7d7f468e614f960a210b9521c9e0b227f464e8e887ea65c43869d35aeea` and
`49b45dae6a94c985583e058685de12b2a27cec1a95b0a2abab4f555b2e0be167`.

## Source-flow telemetry overhead (2026-11-17)

Optimized control `dc4d9e5d26aecbdfa9ca4a5b21f1c66dcd1d524d` and metrics
candidate `77b5c6a15b689c5c72adce6a732df598380a1569` used the same pre-staged
canonical Excalidraw spec, bundle and package roots on wyrm2. The spec tree
matched the control checkout. Each pipeline ran its own projector, analyzer and
saved-analysis apply; no artifact crossed engines. Build/staging were outside
the measured interval. Both builds used `-c opt`, `RAYON_NUM_THREADS=4`, a
16-GiB address-space limit and GNU time, without CPU/heap profiling. Metrics
used `OTEL_METRIC_EXPORT_INTERVAL=1000`.

Three interleaved triples varied baseline/disabled/enabled order. The earlier
preliminary build/triple is excluded. Each value below is the median of three
complete project/analyze/apply runs; wall and CPU are sums within each run,
and RSS is the maximum phase process RSS within each run.

| Build            | Pipeline wall (s) | Change | CPU (s) | Peak RSS (KiB) |
| ---------------- | ----------------: | -----: | ------: | -------------: |
| Control          |             15.77 |      — |   23.26 |         721372 |
| Metrics disabled |             16.58 |  +5.1% |   23.34 |         721428 |
| Metrics enabled  |             17.03 |  +8.0% |   24.17 |         721596 |

Enabling export on the same candidate added 0.45s (+2.7%) to the observed wall
median and 0.83s (+3.6%) to CPU. Relative to control, disabled CPU changed +0.3%
and enabled CPU +3.9%. The wall ranges below overlap, so the +5.1% disabled
wall difference should not all be attributed to executed instrumentation.

Per-run peak RSS ranges were 719992–721444 KiB for control,
721116–816012 KiB with metrics disabled and 721264–818456 KiB with metrics
enabled. Both candidate modes had one higher-memory run; the close medians
do not establish that peak memory overhead is negligible. CPU ranges were
23.17–25.27s, 22.70–25.09s and 23.79–24.40s respectively.

All nine runs emitted the same 156 JavaScript files and byte hashes (manifest
SHA256 `5fedebc4d78e802a13d7a2d3324c08eb7192a2d0716a3cee405d0c18b81be144`).
Wall ranges were 15.61–17.68s, 15.59–19.84s and 16.00–17.48s respectively;
this is a bounded regression check, not a stable estimate of engine speedup.
The observed medians meet the requested 10% bound. Subsequent integration adds
the independently measured initializer index above; its combined check remains
separate from these exact candidate measurements.

The [combined initializer-index/metrics integration check](https://app.buildbuddy.io/invocation/e6f09b93-5d03-49c8-9ecc-bff95ca46949)
passed the CPU-profiled Excalidraw benchmark and all three focused CLI test
targets. Its raw project/analyze/apply OTLP files were downloaded from BuildBuddy
and parsed successfully (1/21/8 records). This checks integration and artifact
delivery; its profiled duration is not part of the unprofiled overhead estimate.

Optimized builds: [control](https://app.buildbuddy.io/invocation/1b7afbc7-bf6f-4439-9728-23f8bb0a855c),
[candidate](https://app.buildbuddy.io/invocation/f5c52e09-548b-4496-b154-ddc01303ad4a).
Raw commands, GNU time files, output manifests and enabled-run OTLP JSONL are
under `/tmp/agents/2026_11_16/debundle-performance-workflow/.git/task-scratch/metrics/`,
runs `baseline-{2,3,4}`, `candidate-off-{2,3,4}` and `candidate-on-{2,3,4}`.
These temporary receipts can expire; the public benchmark's raw BuildBuddy
outputs are the persistent capture path.

A separate 120-second, 16-GiB/no-swap private web analysis probe retained 102
complete OTLP records while stopped inside the program value-flow solve. Its
last sample counted 147,981,683 reads, including 136,940,294 property reads and
108,129,403 absent property reads; 87,247,580 new and 60,734,101 repeated
subscriptions; 2,773,290 fact rows, including 2,486,000 properties; and
107,081,003 wake attempts, of which 106,901,473 coalesced. The systemd unit
reported a 11.2-GiB memory peak and 114.328 CPU seconds before the runtime cap.
These are partial-state counters, not completed closure cardinalities or a
heap allocation attribution. They show that new dependency growth as well as
repeated work needs attention; they do not establish the savings from a
particular representation change. Raw receipts are in the task's
`metrics/tana-probe` directory. Full consumer completion remains unverified.

### Source-set breadth behind the private-consumer fanout

A temporary, count-only attribution build on metrics head
`f2e676a2f6fcc109ee72cb631c60ac7c95640804` wrapped existing fact-context reads
and proposals; it added no semantic reads or transfers. BuildBuddy receipt:
[optimized attribution build](https://app.buildbuddy.io/invocation/90e6840e-89cf-461e-949e-1cd3bf63084a).
Analyzer SHA256:
`ca032bbb7c449bdfc3701a1d61afbe17383c14513f7b54878d1a78e70853dd71`.
The temporary diagnostic code is excluded from production. Snapshots retain
per-event counts and field-aggregated key counts, not every receiver/field key;
reporting happens at exponentially spaced operation-count boundaries. These
are attribution runs, not native timing comparisons.

The same analyzer completed Excalidraw analysis and stopped Tana analysis at
120 seconds with a 16-GiB/no-swap cap. The last complete Tana diagnostic
snapshot was at 128 million reads; it does not represent the exact cap state.

| Source-flow observation          | Excalidraw, completed | Tana, partial snapshot |
| -------------------------------- | --------------------: | ---------------------: |
| Source event rules               |               115,373 |                639,898 |
| Event executions                 |               169,653 |                803,607 |
| Read calls                       |             1,204,573 |            128,000,000 |
| Largest observed origin set read |                   228 |                 20,430 |

These older widths include the separate unknown alternative as one element;
the later transfer witnesses count concrete origins and unknown separately.

Tana's hottest field groups included `stopPropagation` (8,620,545 reads, all
absent), `preventDefault` (7,669,487, all absent), `current` (5,045,676),
`target` (3,511,320) and `children` (3,491,222). Both inputs' hottest event was
a call reading the same React input fields: `value`, `defaultValue`, `checked`,
`defaultChecked`, `type` and `name`. Large sets also reached React element
creation and a generic deep-merge helper's formal arguments. This is evidence
of much broader source alias relations on Tana, not merely more solver rounds.
It does not yet identify the first transfer that makes those sets broad.

The current source relation merges calls into one cell per formal and one
return cell per function, and uses allocation-site object identities. A member
read watches the selected field on every possible receiver, including absent
fields. Broad shared sets therefore create many distinct dependencies and
bitmap joins; faster hashing alone does not remove that relation. `Top` is an
unknown alternative in this relation, not an instruction to enumerate every
allocation. The interpreter's precision-loss/global-reach fallbacks are later
than the phase reached by this capped run.

The producer-census checkpoint and transfer witnesses below continue this
attribution. Do not infer a completed private-consumer run from these partial
counts. The task's raw commands, metadata, count
snapshots and OTLP files are under
`/tmp/agents/2026_11_17/tana-flow-cause/{excal-attribution,tana-attribution-2}`;
these local receipts can expire and contain private source identities.

## Cause-counter validation before the producer census

The cause counters landed in
[#17262](https://github.com/agentydragon/ducktape/pull/17262). The exact measured
candidate is `b6a87859f32edd196043364f8553a041b67cba3b`, before #17266, compared with its parent
`fb69264e500ac689ae425b2bf5842ce7932a72f5`. Optimized build receipts:
[parent](https://app.buildbuddy.io/invocation/a87f865d-01ed-4f9f-a88f-dbaca71b3fed),
[candidate, CLI tests and public Excalidraw benchmark](https://app.buildbuddy.io/invocation/5bcd6867-dd79-4308-bf95-165deee98b37).
Both behavioral test targets and the public benchmark passed; its raw GNU time
outputs were inspected separately from the local comparison below.

Three unprofiled, interleaved control/disabled/enabled pipeline triples used the
same frozen Excalidraw source/spec/package inputs as the preceding measurements,
`RAYON_NUM_THREADS=4`, GNU time and a 16-GiB address-space limit. Each pipeline
ran its own projector, analyzer and saved-analysis application. Build and staging
were outside the measured interval. Medians sum phase wall/CPU within a run and
take the maximum phase RSS.

| Build                   | Pipeline wall (s) | Change | CPU (s) | Peak RSS (KiB) |
| ----------------------- | ----------------: | -----: | ------: | -------------: |
| Parent                  |             15.88 |      — |   23.37 |         721712 |
| Cause counters disabled |             16.37 |  +3.1% |   24.18 |         720988 |
| Cause counters enabled  |             16.40 |  +3.3% |   23.28 |         723208 |

All nine runs emitted the same 156 JavaScript files and byte hashes. Wall
ranges were 15.78–19.76s, 16.12–17.46s and 16.01–16.74s respectively. The observed
medians meet the 10% regression guard; overlapping ranges and differing CPU
medians do not establish a precise instrumentation cost. Raw tool identities,
commands, GNU time, OTLP and output manifests are under
`/tmp/agents/2026_11_17/tana-flow-cause/{control,off,on}-{1,2,3}`.

A separate 120-second, 16-GiB/no-swap Tana probe used the candidate with metrics
and no temporary attribution instrumentation. It retained 100 complete OTLP
records and remained inside `solve_program_value_flow`: interpreter precision
losses had not been reached. The last export counted 143,819,628 reads and
85,968,114 new subscriptions. Source-flow unknown-contribution decisions were:

| Cause                  |       Count |
| ---------------------- | ----------: |
| Missing property       | 105,537,730 |
| Non-object receiver    |  46,258,623 |
| Dynamic property       |   6,303,846 |
| Non-function callee    |   4,712,272 |
| Undeclared binding     |      54,573 |
| Unsupported expression |      54,461 |
| Opaque import          |         633 |

These count analysis decisions, including projections of already-unknown
receivers/callees, not distinct source sites or JavaScript runtime events.
They locate the repeated conservative projections; they do not identify the
first transfer that creates the broad source alias sets. The unit reported
116.102 CPU seconds and 11.2 GiB peak memory before the runtime cap. No completed
analysis or application is claimed. Receipts are in the task's
`tana-cause-counters/`; local scratch can expire.

## Property-producer census checkpoint (2026-11-17)

The source-flow census omits watchers only for exact property keys with no
possible producer in the completed event list. Fixed object/slot writers and
a conservative global dynamic-writer slot set keep all potentially changing
reads subscribed, including initially empty writes. Missing reads still yield
`Top`. The invariant and construction bounds are in
[Source property producer census](../docs/inference.md#source-property-producer-census).

The controlled experiment adds census commit
`080abbb205699e4c1938b1fe7cee35fc84bcc07f` to metrics code
`d23e4af5899df9335823b18cdc8de762ec222f93`, resolving the diagnostics/test
integration. Control `f2e676a2f6fcc109ee72cb631c60ac7c95640804` differs from
that metrics revision only in documentation. Optimized builds:
[control](https://app.buildbuddy.io/invocation/85513e03-a35a-42f0-9488-36a5ddcc9376),
[census integration](https://app.buildbuddy.io/invocation/2bc0cce3-c32e-4217-89ed-ab568f4fb574).
The latter also passed the analysis library build, analysis tests, metrics CLI
and program-value-flow CLI tests. Subsequent rebase incorporates devel's
independent operand-sequencing refactor; the measurements below describe the
controlled integration, not an isolated measurement of that refactor.

Three interleaved optimized Excalidraw cycles used the same frozen corpus,
GNU time, four Rayon threads and 16-GiB address-space bound as the telemetry
comparison. Each configuration ran three fresh project/analyze/apply pipelines.
Enabled runs used one-second OTLP exports. Median wall/CPU are pipeline sums;
RSS is the median of each pipeline's maximum phase RSS.

| Configuration    | Wall (s) | CPU (s) | Peak RSS (KiB) |
| ---------------- | -------: | ------: | -------------: |
| Control disabled |    18.82 |   25.65 |         720384 |
| Census disabled  |    17.58 |   24.46 |         712316 |
| Control enabled  |    16.88 |   24.64 |         723156 |
| Census enabled   |    17.21 |   24.70 |         713156 |

Observed wall medians changed −6.6% disabled and +2.0% enabled, within the 10%
regression bound. All twelve runs emitted the same 156-file JavaScript manifest
as the telemetry comparison. Wall ranges were 18.10–26.20s, 15.73–30.77s,
16.59–28.61s and 16.41–34.33s respectively. Both variants slowed substantially
in the final cycle; these noisy observations do not establish a stable speedup.
Peak RSS ranges were 719972–720580, 712184–809276, 718952–816044 and
712788–714220 KiB respectively.

Separate sequential private analysis probes reused the same frozen projection
for diagnosis. Each had a 120-second wall cap, 16-GiB cgroup memory bound, no
swap, four Rayon threads and one-second OTLP exports. Both stopped within the
program value-flow solve. Last exported counts and systemd receipts were:

| Observation           |   Control |   Census |
| --------------------- | --------: | -------: |
| Value subscriptions   |  84768848 | 63661979 |
| Reads                 | 139546959 | 99135843 |
| Fact rows             |   2731403 |  2542207 |
| Event evaluations     |    806666 |   802668 |
| Unit peak memory      |  11.1 GiB |  8.8 GiB |
| Unit CPU time         |  112.598s | 112.663s |
| Complete OTLP records |        97 |       89 |

The lower retained relation and memory peak occurred at different partial
states. They neither establish completed-solve memory savings nor an
end-to-end speedup. Fresh projection, passing-spec application and runtime
checks within 600 seconds/16 GiB remain unverified.

Comparing the periodic samples at matching propagation progress gives stronger
evidence for the subscription reduction. The closest control sample to the
census endpoint has exactly the same 2,542,207 fact rows, including identical
per-family row counts. It has 802,723 event evaluations versus 802,668 for the
census, and 78,293,225 subscriptions versus 63,661,979: 14,631,246 fewer
subscriptions (18.7%). Reads are 123,622,950 versus 99,135,843. These are
periodic, batched snapshots rather than identical solver checkpoints, but the
drop is not explained just by the different timeout endpoints. The receipts
are in `census-matched-progress.json` and the two `*-samples.json` files below.

Raw commands, GNU time files, OTLP, manifests and tool identities are in
`/tmp/agents/2026_11_16/debundle-performance-workflow/.git/task-scratch/metrics/`:
`census-{control,census}-{off,on}-excalidraw-{1,2,3}`,
`census-control-tana-2`, `census-candidate-tana`, and the two
`census-*-comparison.json` receipts. These temporary files can expire.

## First origin-breadth transfers (2026-11-17)

Temporary optimized instrumentation on devel
`98fcd64042fa071bde2ce043c4b402b1a036e6d9`, including the merged property
producer census, records actual fact-join widths at the first 1,024/4,096/16,384
concrete-origin crossings. It observes existing reads and joins without adding
semantic reads, dependencies or proposals. Each witness keeps its event,
before/incoming/after cardinalities, unknown separately, twelve sampled origins
and at most eight largest inputs read during that evaluation. Cached invocation
operands can produce a witness with no new reads in that evaluation; the event's
argument expression remains available. This is bounded attribution, not a
per-origin provenance graph or a native timing measurement.

Build receipts:
[first trace](https://app.buildbuddy.io/invocation/70554d3b-4393-485e-8c7c-1d7005073c32),
[narrowed trace](https://app.buildbuddy.io/invocation/d6be9851-002e-4e05-b72a-c7450bc84d99).
Both probes reuse the frozen private projection under a 120-second wall limit,
16-GiB/no-swap cap, four Rayon threads and one-second OTLP exports. Both stop
inside source value flow. The first reaches its 20,000-record diagnostic limit;
the second excludes most property-union carriers and retains 12,837 witnesses
through evaluation 809,739 without reaching that limit.

Representative ordered witnesses from the narrowed trace:

| Destination                                           | Before | Incoming concrete origins |  After |
| ----------------------------------------------------- | -----: | ------------------------: | -----: |
| JSX props formal, one literal argument                |  1,023 |                         1 |  1,024 |
| Same formal, later literal argument                   |  4,095 |                         1 |  4,096 |
| JSX element's `ref` field                             |      0 |                     9,709 |  9,709 |
| Generic property-lookup local                         |      0 |                    17,040 | 17,040 |
| Generic property-lookup return                        |     11 |                    17,047 | 17,047 |
| Deep-merge formal, called through a throttle variable |    483 |                    17,043 | 17,134 |
| Another dynamic member-read local                     |     59 |                    20,429 | 20,429 |

These identify several distinct sources of breadth:

- A shared JSX formal legitimately accumulates one object from each source call.
  This starts as many small contributions, rather than one mysterious huge
  incoming set. The helper later reuses its props parameter for a ref value.
  The flow-insensitive binding cell retains its entry values, so writing that
  parameter into the returned element's `ref` also writes the props objects.
  The trace observes 9,709 concrete origins in that field, rising above 18,000
  later. Shared allocation-site identities and dynamic member reads then
  distribute broad field unions.
- A shared default-export helper returns its argument or its `default` member.
  All calls feed one formal and one return cell. A variable authored as a
  throttle helper consequently has 47 concrete origins in its callee set.
  Its invocation actually contributes to the deep-merge and generic
  property-lookup formals. This is cross-call pollution through the shared
  wrapper, established by the transfer witnesses and source mapping.
- A generic property-lookup helper reads a dynamic key from its shared object
  formal. Its local acquires 17,040 concrete origins through object field-union
  facts, its shared return spreads those values to callers, and a subsequent
  call feeds them back into unrelated helpers' formals. The widest previously
  observed local similarly grows from 59 to 20,429 concrete origins in one
  dynamic member projection, with unknown retained separately.

This explains concrete extra call edges and broad carriers before the later
interpreter fallback machinery runs. It does not attribute all subscriptions
or CPU time to any one mechanism. A narrower return summary at a call site
must prove its eligibility and preserve conservative unknowns; changing queue
order, dropping possible origins or naming a library is not such a proof.

### Isolating the shared default-wrapper return

An opt-in, capture-specific ablation changes only the observed default wrapper's
return-value expression: each call reads its own argument and that argument's
`default` field, rather than the pooled return cell. Original invocation/effect
collection remains. This is an experimental abstract-transfer change, not a
production recognizer or a correctness proof for general specialization.
[Optimized ablation build](https://app.buildbuddy.io/invocation/7d253961-79f5-49f4-8759-be6186ee66c6).

Under the same 120-second/16-GiB bounds, the deep-merge formal never crosses
1,024 concrete origins; the control's first crossing is the throttle-variable
call that takes it from 483 to 17,134. Other broad sets remain, reaching 19,956
concrete origins in recorded witnesses. This isolates one source of pollution
without explaining the entire remaining relation.

Last complete OTLP snapshots and systemd receipts:

| Observation       | Trace control | Wrapper ablation |
| ----------------- | ------------: | ---------------: |
| Subscriptions     |    73,099,653 |       59,904,861 |
| Reads             |   125,524,389 |      100,617,526 |
| Fact rows         |     2,854,386 |        2,512,302 |
| Event evaluations |       809,649 |          799,808 |
| Unit peak memory  |       9.7 GiB |          8.5 GiB |
| Unit CPU          |      118.895s |         115.505s |

Both stop inside source value flow. These are different partial abstract states,
with deliberately different transfers, not matched completed pipelines. The
memory/count differences are exploratory evidence, not a production speedup or
soundness result. A general implementation must prove eligible callees and
return summaries, preserve conservative unknown/effect behavior, and pass the
normal CLI/runtime and Excalidraw performance checks. The active performance
plan owns that work and the outstanding 600-second full-pipeline proof.

Raw commands, source-coordinate metadata, JSONL witnesses, OTLP, tool identities
and temporary diagnostic patches are under
`/tmp/agents/2026_11_16/debundle-performance-workflow/.git/task-scratch/origin-breadth/`.
The witnesses contain private source identities and stay local. These scratch
receipts can expire; the temporary diagnostic code is excluded from production.

## Bounded call-return relationships (2026-11-17)

The production source recognizer and its substitution proof are documented in
[Bounded call-return relationships](../docs/inference.md#bounded-call-return-relationships).
It preserves ordinary invocation effects while keeping identity/default-wrapper
result origins specific to the actual argument. It adds no precision setting or
author premise. A neutral identity fixture now permits an independent initializer
to move; the default-wrapper fixture removes the unrelated callback edge while
retaining its separate guard/effect restriction.

The baseline [CLI reproduction](https://app.buildbuddy.io/invocation/2fab23ed-f2a8-4ee2-9da5-c163b49bcc7d)
rejects that identity cut and fails the default-wrapper negative-edge assertion.
The final [fresh/saved CLI regressions](https://app.buildbuddy.io/invocation/40fbb7a0-29d7-4086-a069-2f41fcee1372)
pass identity/arrow extraction, wrapper/nested-call origin checks and emitted
Node execution. Nested-call extraction still needs an independent effect proof,
so its regression uses an inline assignment. The wrapper safety, analysis and
fanout benchmark targets passed in the earlier
[candidate invocation](https://app.buildbuddy.io/invocation/b036a138-eb7b-4e75-931f-a021d34cb744);
that invocation's superseded nested-call extraction test failed.

Final cumulative source-flow counters from the raw OTLP artifacts:

| Wrapper calls | Baseline reads | Candidate reads | Baseline property reads | Candidate property reads | Baseline subscriptions | Candidate subscriptions |
| ------------: | -------------: | --------------: | ----------------------: | -----------------------: | ---------------------: | ----------------------: |
|           128 |        101,511 |           3,207 |                  16,384 |                      128 |                 83,844 |                   2,564 |
|           256 |        399,623 |           6,407 |                  65,536 |                      256 |                331,524 |                   5,124 |

The wrapper baseline is the tests-only
[invocation](https://app.buildbuddy.io/invocation/9ee3b1d0-3245-48b4-a9e4-098017a24977)
on engine `f8d7c275c3`; candidate engine `2dcb931639` uses the same base and
optimized build settings. The identity benchmark's candidate property reads are
256, 1,024 and 4,096 for 128, 512 and 2,048 calls respectively, matching its direct
projection controls. Total analysis is not claimed linear: ordinary formal and
effect propagation still runs. Raw benchmark JavaScript, metrics and Node output
checks are retained in BuildBuddy test outputs.

The same candidate binaries were built in
[BuildBuddy](https://app.buildbuddy.io/invocation/9be1a44c-cea6-4f7c-a33b-f10abcaf96d4)
and tested against the frozen private web consumer at revision `563b3e6455`.
Fresh canonical projection completed in 11.64 seconds with 1,425,840 KiB maximum
RSS. Whole-program analysis was then OOM-killed: the native systemd receipt reports
16 GiB peak memory, zero swap, 472.651 seconds whole-pipeline wall time and
466.758 seconds CPU. The configured limit was 600 seconds / 16 GiB, with four
Rayon workers and metrics disabled; compilation and staging were outside it.
No saved analysis was reused. Passing-spec application, gate checks, translation
validation and emitted-runtime checks were never reached. This does not establish
an assignment rejection or satisfy PF0's completion condition.

The run is not a matched private-corpus speedup comparison: it establishes the
candidate's remaining failure. It also gives no reason to optimize projection
ahead of the unfinished analysis. Keep PF0 open and attribute the remaining
analysis growth before broadening the admitted summary shapes. Commands, binary
hashes, native receipts and fresh projection are in
`/tmp/agents/2026_11_16/debundle-performance-workflow/.git/task-scratch/default-export-wrapper/tana-fresh/`;
the terminal systemd receipt is the adjacent `tana-unit.stderr`. These local
private-input receipts can expire.

A separate 120-second Analyze prefix used that fresh projection, the same
candidate, raw OTLP at one-second intervals and `perf_wrapper.sh` (`cycles:u`,
49 Hz, DWARF call chains). It stopped intentionally, with 8.8 GiB unit peak
memory. The last complete metrics snapshot still had 110,769,063 reads,
101,186,614 property reads and 62,976,464 subscriptions. The flat CPU profile
attributed 26.56% self time to Roaring `Store::is_subset`, 12.01% to the
value-expression fact-read closure and 7.87% to `BTreeMap<FlowKey, SetValZST>::insert`.
This locates substantial remaining work in source-flow property relations and
set/dependency bookkeeping. It does not identify the late OOM allocation or
prove which remaining source construct generates those relations. Raw metrics,
`perf.data` and standard perf reports are under `tana-diagnostic/` in that
scratch root. Use a concrete origin-growth witness to choose the next bounded
semantic summary; coordinate the subset/dependency work with PF7/PF8.

### Matched Excalidraw regression check

Six baseline/candidate pairs per metrics mode ran sequentially on the same host,
alternating pair order. Both optimized tool sets used the same pre-staged inputs,
four Rayon workers, GNU `time -v`, a 16 GiB per-phase address-space bound and a
120-second per-phase timeout. Compilation and staging were outside measurement.
The baseline binaries came from
[BuildBuddy](https://app.buildbuddy.io/invocation/e6c24e96-5266-4fe6-8f77-f3ba6618dc41).

| Whole pipeline | Median wall baseline → candidate | Median paired wall ratio | Paired wall ratio range | Median CPU baseline → candidate | Maximum RSS baseline → candidate |
| -------------- | -------------------------------: | -----------------------: | ----------------------: | ------------------------------: | -------------------------------: |
| Metrics off    |                15.890 → 16.345 s |                   1.0028 |         0.9505 – 1.0522 |               23.785 → 24.165 s |            808,772 → 715,212 KiB |
| Metrics on     |                15.540 → 16.135 s |                   1.0403 |         0.9594 – 1.0683 |               23.235 → 23.820 s |            810,712 → 813,104 KiB |

Ratios are computed within each adjacent baseline/candidate pair; their median
differs from the ratio of the separate wall-time medians. Every observed pair
and both median comparisons are below the requested 10% slowdown threshold.
This is a measured regression check, not a bound on every host/run. The initial
three pairs varied enough to warrant the additional three; all samples remain
included. CPU is summed user and system time.

All 24 pipelines passed the normal gate and emitted the same 156 JavaScript
files, with byte-identical manifests (`5fedebc4d78e802a13d7a2d3324c08eb7192a2d0716a3cee405d0c18b81be144`).
Raw GNU time files, commands, metric-enabled JSONL and output manifests are in
the same scratch root's `excalidraw-paired/`; binary hashes are in
`baseline-tools/` and `candidate-tools/`. The runs use production code identical
to implementation commit `61ce1b518a`; intervening base changes only touched
Debundle tests or other projects.

## Remaining source-flow growth (2026-11-17)

The bounded-return implementation at `5b27ed995c` still reaches the same large
record/parameter pools. Four diagnostic runs used the frozen private inputs
above, the same fresh projection, optimized analyzers, four Rayon workers,
one-second OTLP export, and a 120-second / 16 GiB unit limit with no swap.
Temporary instrumentation records bounded width-crossing witnesses from existing
reads and joins; it adds no semantic facts or dependencies. These are partial
Analyze runs, not fresh whole-pipeline acceptance or matched throughput results.

| Transfer variant                                 | Last sampled evaluations |       Reads | Subscriptions | Unit peak memory |
| ------------------------------------------------ | -----------------------: | ----------: | ------------: | ---------------: |
| Bounded returns                                  |                  803,816 | 118,433,667 |    65,781,195 |          9.0 GiB |
| Return summaries disabled                        |                  809,645 | 125,494,350 |    73,085,334 |          9.7 GiB |
| One reassigned ref read corrected                |                  803,835 | 116,672,410 |    65,455,110 |          9.0 GiB |
| One counted-loop read restricted to numeric keys |                  803,110 | 115,944,838 |    64,967,988 |          9.0 GiB |

Every run timed out. The disabled-summary control uses the same implementation
with recognition disabled; it is not an exact historical binary. The other two
ablations target one observed transfer each and are not production recognizers.
They cannot establish a new source-level admission rule. Different endpoints
make the final cumulative counts unsuitable for claiming runtime savings.

The mechanism is visible before those endpoints:

- With and without bounded returns, a shared props formal first reaches 9,709
  origins in the returned record's ref field. The helper overwrites that formal
  before returning; the flow-insensitive binding also contains pre-assignment
  props objects.
- Correcting that one field read removes its first broad propagation, but the
  record's props field still brings the all-field union to 10,655 origins. The
  ref field later grows broadly again. Ref reassignment alone is insufficient.
- A recursive traversal reads `value[index]` in a counted loop. Treating this
  particular read as numeric removes its broadening. The next witness is another
  dynamic read, `items[items.length - 1]`, which consumes the same pooled named
  fields. The existing primitive-result analysis can prove the latter key
  numeric without interpreting the helper or assuming an intact array guard.

The actionable domain distinction is numeric property keys versus arbitrary
computed keys. It does not justify suppressing callbacks, assuming records are
arrays, skipping getter/coercion effects, or treating a bare loop counter as
numeric without a reaching-value proof. The normal unknown alternative and late
property writes must remain represented.

A separate late heaptrack capture of the production bounded-return candidate
was OOM-killed at the 16 GiB cgroup limit after 522.919 seconds (609.799 CPU
seconds, including profiler overhead). Its last OTLP snapshot had 633,023,087
reads, 104,125,380 subscriptions and 2,678,345 property facts; source-flow analysis
had not finished. Offline `heaptrack_print` completed under 8 GiB / 120 seconds,
but reported a malformed final line. Its parsed-prefix peak was **12.04 GB of
tracked heap**, with **14.76 GB profiled RSS**; heaptrack's GB/MB units are decimal,
unlike the cgroup GiB limit. These are not an exact terminal RSS partition.

The largest allocator groups were hash-table capacity (3.98 GB), B-tree node
insertion (2.18 GB), vector growth (1.83 GB), and another map/set insertion group
(1.33 GB). Two B-tree subpaths through expression/property reads account for
about 772 MB within the 2.18 GB group and match cached expression dependencies.
Generic/inlined frames do not uniquely split the remaining bytes between fact
maps, forward watcher sets and reverse adjacency vectors. Bitmap clone bodies
account for only 66.73 MB in this capture. The measured storage target is the
dependency/fact relation, not a demonstrated multi-gigabyte bitmap-copy owner.

The diagnostic builds are [control](https://app.buildbuddy.io/invocation/22631490-eea7-467c-82a2-9d970207d38e),
[ref transfer](https://app.buildbuddy.io/invocation/fd80b155-8060-447a-935b-6af6697cd5ea),
and [numeric transfer](https://app.buildbuddy.io/invocation/ecc846f8-4c65-4f89-8f85-7ae0970a31b4).
Private witnesses, exact patches, binary hashes, commands, raw OTLP, native unit
receipts and compressed heaptrack outputs are under
`/tmp/agents/2026_11_17/tana-remaining-growth/`; these local artifacts can expire.
No diagnostic environment switch or capture-specific ID belongs in production.

### Numeric-key aggregate candidate

The numeric-key candidate uses the existing primitive-result proof and a separate
numeric-property aggregate; it adds no capture-specific rules. The matched base
is `c183e8baf33412f592eb47364e4473d94e52274b`, built in
[BuildBuddy](https://app.buildbuddy.io/invocation/6c54400c-5b61-40f3-9dd6-ec71877aef2d).
The [candidate build](https://app.buildbuddy.io/invocation/722a4147-9132-4468-b5f7-866cc2054b0e)
uses that base plus `candidate-source.patch`, SHA-256
`1090a123d39cb755799cae63fd12f48cced58c09eed81027fc6f5de18175d07e`.
A subsequent visibility correction only exposes the name classifier to sibling
unit tests; it does not change production behavior. Binary hashes are retained
in the scratch root's `baseline-tools/identity.json` and
`candidate-tools/identity.json`.

Fresh private projection passed in 15.14 seconds, with 1,430,428 KiB maximum RSS.
The candidate then exhausted the 16 GiB cgroup limit during analysis:
**475.632 seconds whole-pipeline wall time, 465.077 seconds CPU, zero swap**.
The whole-run limit was 600 seconds, with four Rayon workers, optimized binaries
and metrics disabled. No saved analysis was reused; apply, the passing-spec gate
and emitted-runtime validation were not reached. This is an unsuccessful
completion test, not a matched private speedup measurement.

Three adjacent baseline/candidate pairs per metrics mode ran sequentially on
the same pre-staged Excalidraw inputs, alternating order. Each whole pipeline
includes fresh projection, analysis and apply. GNU `time -v` records each phase;
compilation and input staging are outside measurement.

| Whole pipeline | Median wall baseline → candidate | Median paired wall ratio | Paired wall ratio range | Median CPU baseline → candidate | Maximum RSS baseline → candidate |
| -------------- | -------------------------------: | -----------------------: | ----------------------: | ------------------------------: | -------------------------------: |
| Metrics off    |                  16.49 → 16.03 s |                   0.9721 |           0.9597–0.9884 |                 24.26 → 24.16 s |            808,980 → 710,784 KiB |
| Metrics on     |                  15.68 → 16.33 s |                   1.0089 |           0.9612–1.0722 |                 23.26 → 24.15 s |            810,904 → 806,664 KiB |

Every observed paired wall change remains below the requested 10% slowdown
threshold. All 12 pipelines passed the normal gate and emitted byte-identical
manifests of 156 JavaScript files. This is a measured regression check, not a
universal runtime bound. Raw phase times, commands, output manifests and
metrics-enabled JSONL are in `excalidraw-paired/` under the scratch root above;
private receipts are in `tana-numeric-fresh/` and its adjacent stderr file.

The [analysis/library and existing CLI checks](https://app.buildbuddy.io/invocation/de41f686-da6b-4752-91ce-dcff60bc429e)
passed; that invocation's first numeric CLI fixture failed. Reduction exposed
an independent `unsupported_operation(member_read)` receipt in synchronous
execution followed by global reach, so the computed-key safe cut remained
rejected despite improved source flow. The final literal-key behavioral control
and unsafe-cut refusals pass on both the
[baseline](https://app.buildbuddy.io/invocation/f0b63a1a-bdb1-4dce-8dbe-15ed1694e867)
and [candidate](https://app.buildbuddy.io/invocation/726c779b-b545-4050-9e22-928f977e61eb).
These are safety controls, not a demonstrated newly accepted cut. Unit tests
pin the narrower origin sets, independent reference-solver agreement, late
numeric writes, program projection parity and constant aggregate-read work as
unrelated named fields grow. Neither those tests nor the Excalidraw check closes
the private completion task.

The [final CLI test](https://app.buildbuddy.io/invocation/0eba998e-356c-4d12-9c6b-f804b5739af6)
also retains that independent blocker: both computed numeric variants reject
the cut with the unsupported member-read/global-reach receipt, while executing
the emitted candidate yields the same output as the original program. This
explicit current-limitation control must change when the execution proof lands;
it is not evidence that the cut is semantically unsafe.

A separate 120-second analyzer prefix with one-second OTLP and 49 Hz perf
sampling still remained in source-flow solving. The last complete snapshot had
94,347,028 reads and 54,757,140 subscriptions; the unit peaked at 9 GiB. The
flat report attributes 36.09% self CPU to Roaring `Store::is_subset`, 9.81% to
the expression fact-read closure and 5.92% to dependency B-tree insertion.
Different solver progress prevents comparing prefix totals as throughput gains.
The wrapper completed normally after the analyzer's deliberate timeout; that
wrapper success is not an analysis pass. Raw metrics, perf data and standard
reports are in `numeric-profile/` under the same scratch root.

### Counted-loop numeric-read candidate

The next candidate retains a per-member numeric certificate for a function-local
counted loop. Its proof and exclusions are in
<../docs/inference.md#counted-loop-read-certificates>. It also covers a counter
reused outside the loop: only the certified read changes. A numeric initializer
alone is not a global type for the binding.

The program-query regression
`counted_loop_certificate_survives_program_expression_queries_only_at_its_sites`
[fails on the numeric-aggregate baseline](https://app.buildbuddy.io/invocation/bd697b55-bf32-408b-9760-5a1c763a02d6):
the inside-loop read incorrectly includes the unrelated named-field callback.
The [combined corrected checks](https://app.buildbuddy.io/invocation/078e2a50-8bef-4b2c-8679-02f69478dfae)
pass all 446 analysis tests (three pre-existing ignored tests), the analysis
library checks, and the numeric-member, program-value-flow and return-summary
CLI suites. Controls cover writes, captures, dynamic scope, parameter aliases,
later reuse and complete-span membership for nested members with the same start
byte. The CLI unsafe cases still reject and their emitted candidates demonstrate
the expected TDZ. The independent computed-read execution fallback remains.

The [optimized build](https://app.buildbuddy.io/invocation/d119db39-bc1e-4292-9993-a79a7a5ed01c)
uses parent `5841f1f4564df4b4df935f97c94432d50673658e` plus
`loop-source.patch`, SHA-256
`9b9c77966e7eef508eb472d256f943af646124b19ad11df3d169a378315924af`.
Binary hashes are in `loop-tools/identity.json` under the scratch root above.
The fresh private projection passed in 14.44 seconds, with 1,450,236 KiB
maximum RSS. The whole pipeline then hit the 16 GiB cgroup memory cap after
**467.833 seconds wall time and 458.224 seconds CPU**, with no swap. Analysis
did not complete, and application and runtime checks were not reached. This
still fails the 600-second / 16-GiB completion requirement; it is not a measured
private speedup. The run used four Rayon workers and disabled metrics, with
fresh projection and no saved analysis. Receipts are in `tana-loop-fresh/` and
its adjacent stderr file under the same scratch root.

Three interleaved baseline/candidate pairs per metrics mode used the same
optimized baseline and pre-staged Excalidraw inputs as the numeric-key experiment.
All 12 fresh projection/analysis/apply pipelines passed and emitted byte-identical
manifests of 156 JavaScript files. Raw phase times, commands, manifests and JSONL
are in `excalidraw-loop-paired/` under the scratch root above.

| Whole pipeline | Median wall baseline → candidate | Median paired wall ratio | Paired wall ratio range | Median CPU baseline → candidate | Maximum RSS baseline → candidate |
| -------------- | -------------------------------: | -----------------------: | ----------------------: | ------------------------------: | -------------------------------: |
| Metrics off    |                  16.07 → 15.90 s |                   0.9894 |           0.9888–1.0035 |                 23.85 → 23.44 s |            713,972 → 807,676 KiB |
| Metrics on     |                  15.66 → 15.89 s |                   1.0089 |           0.9778–1.0775 |                 23.25 → 23.68 s |            811,304 → 810,096 KiB |

Every observed pair remains below the requested 10% wall-time slowdown ceiling.
Memory is reported separately: the metrics-off maximum RSS is higher in the
candidate. This regression check does not close the private completion gate.

A separate 120-second production-analyzer prefix used one-second OTLP and
49 Hz perf sampling. Source-flow solving had not completed. The last complete
snapshot recorded 104,817,363 reads and 58,495,545 subscriptions; 97,062,633 reads
were named-property lookups, including 70,595,464 absent reads. The flat profile
attributes 34.99% self CPU to bitmap `Store::is_subset`, 9.81% to the expression
fact-read closure and 6.60% to dependency B-tree insertion. Thus the remaining
observed work is not predominantly numeric-aggregate lookup. These prefix totals
are not a throughput comparison: solver progress differs between runs.

The profile wrapper completed after the deliberate analyzer timeout in 124.923
seconds, with 118.326 seconds CPU and a 9.4-GiB cgroup peak; wrapper success is
not an analyzer pass. Raw metrics, perf data and standard reports are in
`loop-profile/`. Choosing the next semantic change still requires a transfer
witness from this combined candidate, including confirmation that the intended
counted-loop read lowered to `NumericMember`.

## Candidate-containment graph materialization (2026-11-17)

A late bounded run identifies the terminal allocation more narrowly than the
source-flow prefix profiles. The observed production analyzer is the combined
numeric-key/read-certificate candidate, implementation `f9c040d9fb3e4fdc4240ceb2821abaafce13e5c0`
([#17359](https://github.com/agentydragon/ducktape/pull/17359),
[#17374](https://github.com/agentydragon/ducktape/pull/17374)). Its binary SHA-256 is
`62c303cbf7c6bd4a44c574e9352034c38b02e781c660aabd6af9404abbd4318a`.
The candidate-containment implementation is unchanged between that candidate and
fresh baseline devel `5b9b7e54e59acdd09a0ca8d6a1ecadbbef3d6375`.

The analyzer used the frozen private capture/projection, four Rayon workers,
one-second raw OTLP JSONL, progress markers, a 600-second runtime cap, 16 GiB
memory cap and no swap. A 49 Hz CPU profile was attached for a late 120-second
window. This is an analyze-only diagnostic, not a fresh whole-pipeline acceptance
run or an unprofiled timing comparison. The unit ended with `oom-kill` after
453.606 seconds wall and 445.802 seconds CPU, at the 16 GiB cap.

The progress log records `solve_program_value_flow` ending and
`close_program_value_flow` starting before OOM. The final 30 seconds of the
captured CPU window attribute 72.37% self cost to
`GraphMap<FlowKey>::add_edge`, 8.12% to its edge-index reallocation, and 5.31%
to its node-adjacency entry lookup. The complete 120-second window attributes
31.18% to `add_edge`; its earlier portion still includes source propagation.
The `FlowKey` graph type identifies candidate-containment compilation: the
preceding call-effect closure uses a different graph/key representation.
A second attachment attempted after the first capture found the process already
gone and produced no usable samples.

The completed origin relations are already compressed bitmaps. Expanding each
eligible origin into a `DiGraphMap` edge retains an edge index and both adjacency
directions. SCC closure bounds the number of semantic propagation steps but does
not remove that representation cost. A finite polynomial relation can still
exhaust the memory budget when expanded this way. The next implementation must
reuse the immutable compressed adjacency for graph traversal and avoid retaining
an expanded component-edge relation as well.

Separate observational transfer witnesses confirm the counted-loop certificate
is active at the earlier hotspot. Subsequent broad reads include an index loaded
from a pooled ref cell, a type-guarded index without a retained branch-local type
fact, and numeric fields of shared memoized tuples. One unrestricted selection
joins 11,275 origins, including 10,680 from one shared return-record aggregate.
This explains why improving one numeric read does not close the end-to-end task;
it does not justify another recognizer ahead of the measured graph expansion.

Raw commands, progress, OTLP, native unit receipt, perf data and standard reports
are under `/tmp/agents/2026_11_17/tana-remaining-growth/loop-late-metrics/` and the
adjacent `.stderr` file. The bounded observational witnesses are in
`loop-diagnostic/`; they are private, transient diagnostic artifacts. No private
source or capture-specific dispatch belongs in the implementation.

### Fresh devel control

A fresh optimized control at `5b9b7e54e59acdd09a0ca8d6a1ecadbbef3d6375`
([tool build](https://app.buildbuddy.io/invocation/c2d8132c-900f-4d27-81cc-f5ee7b048a22))
uses identical frozen private inputs, four workers, metrics disabled, progress
markers, a 600-second whole-pipeline limit and 16 GiB with no swap. Projection
completed in 14.26 seconds. Source-value solve completed; candidate closure
started; the unit then ended with `oom-kill` at 516.134 seconds wall and
509.794 seconds CPU, at the 16 GiB cap. Analysis did not finish and passing-spec
application did not run. This independently reproduces the failure on devel;
it is not a timing comparison against the profiled numeric-key candidate.

Raw outputs and the native unit receipt are in the same task root's
`tana-closure-baseline/` and `tana-closure-baseline.stderr`. Binary identities
are recorded in `closure-baseline-tools/identity.json`. An initial setup attempt
failed before executing projection because downloaded artifacts lacked execute
permission; it is retained separately and excluded from these measurements.

### Borrowed-adjacency result

The replacement is published in
[#17390](https://github.com/agentydragon/ducktape/pull/17390).
The [optimized tool build](https://app.buildbuddy.io/invocation/bb6a3126-fa59-4d4e-aebd-24d466628db4)
applied patch digest `1cfd59b5f907485ea64d0a9fac2c58f370035b85bd391d80f5d247debde7d3aa`
to the same devel baseline. Analyzer SHA-256:
`a121c044f6dfdd6855c314552406b0136e681de85edeed90f56ac17a23e304d8`.
This build precedes removal of the now-unused `Values::ids_within` helper;
the executed closure code is unchanged by that deletion. Final library/Clippy,
438 analysis tests (three pre-existing ignored), and the candidate-object escape
and whole-program value-flow CLI suites passed in
[the focused check](https://app.buildbuddy.io/invocation/95ea3e34-898b-4c99-9906-9a2eefaa294b).

The fresh unprofiled private pipeline used the same inputs and limits as the
control. It completed both source solve and candidate closure, then timed out
before `program_value_flow` ended. The native receipt reports 600.235 seconds
wall, 593.837 seconds CPU and 12.1 GiB peak memory. There was no OOM, but analysis
and passing-spec application did not complete. This demonstrates removal of the
observed closure allocation failure, not completion of the private-consumer
acceptance task. The separate profile below attributes the interval after
closure. Raw outputs are in `tana-closure-candidate/` and
`tana-closure-candidate.stderr` under the task scratch root above.

For the public canonical Excalidraw spec, three adjacent baseline/candidate
pairs per metrics mode alternated execution order. All runs used optimized
matching tools, identical staged inputs and four workers, with no overlapping
local consumer run. Metrics-enabled runs used a one-second export interval.
Wall and CPU are summed across projection, analysis and application; peak RSS
is the largest phase RSS observed across each variant's three runs.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 15.73 → 15.56 s                   | 23.21 → 23.17 s                  | 0.9905                   | 0.9644–0.9935      | 810,056 → 808,676                  |
| On      | 15.66 → 16.11 s                   | 23.28 → 23.77 s                  | 1.0088                   | 0.9859–1.0894      | 811,212 → 810,100                  |

All observed pairs are below the requested 10% slowdown threshold. This is a
measured regression check, not a guarantee for every run. All 12 pipelines
passed their normal gate and emitted byte-identical sets of 156 JavaScript
files. Raw GNU time output, commands, OTLP JSONL and JavaScript manifests are
in `excalidraw-closure-paired/`; tool identities are in `closure-baseline-tools/`
and `closure-tools/`. Compilation and input staging are outside the timings.

### Chunk candidate projection

A subsequent diagnostic replays only Analyze with the same optimized closure
candidate and its matching saved input projection. Metrics are disabled, with
four Rayon workers and progress markers. After `close_program_value_flow`
reports completion, standard `perf record` attaches for 30 seconds at 49 Hz
(`cycles:u`, DWARF stacks). The analyzer is then intentionally terminated. The
outer unit has the same 600-second, 16 GiB, no-swap bounds; its receipt reports
383.144 seconds wall, 376.202 seconds CPU and 12.1 GiB peak. This is a bounded
CPU attribution capture, not an analysis completion or timing comparison.

The capture contains 1,466 samples with none lost. Flat symbol self costs are:

| Symbol                             | Self cost |
| ---------------------------------- | --------: |
| `ChunkProgramFlow::candidate_bits` |    30.29% |
| `Arc<RoaringBitmap>::make_mut`     |    25.36% |
| Roaring `Store::insert`            |    22.51% |
| `ChunkProgramFlow::translate`      |    10.76% |

Some unwound call chains contain unresolved small addresses; caller-inclusive
costs are not used as evidence. The flat symbols and phase marker identify
chunk projection. `candidate_bits` remaps a global candidate bitmap afresh;
`translate` invokes it for every foreign allocation and inserts the resulting
local candidates individually. Per-origin reachability projection also creates
fresh bitmaps. Thus distinct origins whose closure summaries share an `Arc`
repeatedly project identical immutable content. The existing whole-value-set
translation cache does not remove repetition within a set or across different
sets using the same candidate summary.

Preserving summary sharing is a representation change: project each retained
source-summary identity once per module and union it once per translated value
set. It does not narrow may-origins or remove the foreign `Top` alternative.
The source-only cost model must still charge unique projected payloads, source
origin visits and bitmap unions; a cache hit is not proof of lower end-to-end
cost. Fresh whole-pipeline and matched Excalidraw measurements remain required.
Raw `perf.data`, standard reports, commands, progress and the explicit stop
receipt are in `post-closure-profile/` under the task scratch root above.

### Shared-projection result

The representation change is published in
[#17405](https://github.com/agentydragon/ducktape/pull/17405).
The [optimized candidate build](https://app.buildbuddy.io/invocation/7580edae-1c4c-4872-b8fb-31568b49cdc6)
applied patch digest `7a874f823894d95b7fedad9a4650f08606dff17dfb6f7c95b0f9f0f09be35f27`
to devel `a255682c02ee6c495d06ee17b26b6776a3ce8b40`, which includes the closure
fix. Analyzer SHA-256:
`3666c20bc9d4d79be0bf67e458d702ba4cf4cf4948198177f101939e81529669`.
Subsequent test-oracle corrections did not change the measured production code.
The [focused check](https://app.buildbuddy.io/invocation/d406ad2f-547a-4691-9859-c49d87afc6e2)
passed the analysis library/Clippy check, 439 analysis tests (three pre-existing
ignored), candidate-object escape CLI suite and whole-program value-flow CLI
suite. Small parsed-source controls compare every projected value row with an
independent set-based oracle and pin summary sharing, union counts and cache
lifetimes. The candidate-summary projection preservation argument is in
[Program value flow](../docs/inference.md#borrowed-candidate-containment-adjacency).

The fresh private pipeline used the same frozen inputs, four workers, metrics
disabled, progress markers and 600-second/16-GiB/no-swap limits. Source solve,
candidate closure and `program_value_flow` all completed. Cross-module purity
preparation completed, then main-chunk analysis started and did not finish.
The native receipt reports a timeout at 600.231 seconds wall, 595.788 seconds
CPU and 12.1 GiB peak memory, with no OOM. Passing-spec application did not run.
This advances beyond the closure-only candidate's stopping point; it does not
establish an end-to-end speedup or meet the completion target. The phase log
has no timestamps, so it does not establish a chunk-analysis duration.
Raw commands, progress and the unit receipt are in `tana-projection-candidate/`
and `tana-projection-candidate.stderr` under the task scratch root above.

The [optimized Excalidraw baseline](https://app.buildbuddy.io/invocation/18b3a43b-8d8c-4488-a5e8-944f1976f5ce)
is the exact same devel revision without the patch. Three adjacent pairs per
metrics mode alternate execution order, using matching toolsets, identical
staged inputs and four workers, with no overlapping local consumer. Metrics
export every second when enabled. Wall and CPU sum projection, analysis and
application; peak RSS is the largest phase RSS across each variant's runs.
Compilation and input staging are outside the timings.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 14.99 → 15.11 s                   | 22.23 → 22.81 s                  | 1.0286                   | 0.8496–1.0727      | 809,284 → 809,228                  |
| On      | 15.26 → 15.30 s                   | 23.08 → 22.83 s                  | 1.0026                   | 0.9665–1.0078      | 713,968 → 714,916                  |

Every observed pair stays below the requested 10% slowdown threshold; the
small sample does not guarantee that bound for every run. All 12 pipelines
passed the normal gate and emitted byte-identical sets of 156 JavaScript
files. Raw GNU time outputs, commands, OTLP JSONL and manifests are in
`excalidraw-projection-paired/`; binary identities are in
`projection-baseline-tools/identity.json` and `projection-tools/identity.json`.

### Completed-value index: public Excalidraw check

[#17413](https://github.com/agentydragon/ducktape/pull/17413) replaces the
completed value map with direct binding/site slots and sorted sparse property
rows. The preservation argument and bounds are in
[Program value flow](../docs/inference.md#borrowed-candidate-containment-adjacency).
The [optimized build](https://app.buildbuddy.io/invocation/035b163c-eefe-4ff9-8867-80e6245e43f8)
uses devel `5d25430e652cfd4caefcb38b3c9cb5bbbb80a979` plus the production patch
in `032783a618edf0f4c52bf356bf89c2ee13b27366`. Analyzer SHA-256:
`c9896715db1bb8ce3f745edee1f394ddbdd74d30d28066094121cd06cb9a4668`.
The baseline reuses the shared-projection candidate binaries above; changes
between that candidate and this base affect only tests and documentation in
Debundle, with no production-engine or toolchain changes.

Three adjacent pairs per metrics mode alternate execution order, with identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. Timings sum fresh projection, analysis and
application; compilation and input staging are outside the interval.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 15.49 → 15.40 s                   | 22.92 → 22.87 s                  | 0.9942                   | 0.7967–1.0125      | 714,236 → 809,152                  |
| On      | 15.46 → 15.57 s                   | 22.86 → 23.37 s                  | 1.0284                   | 0.9761–1.0335      | 714,500 → 809,956                  |

Every observed pair stays below the requested 10% slowdown threshold. The first
metrics-off baseline took 19.38 seconds, compared with 15.49 and 15.21 seconds
for its later repetitions; this sample does not establish a speedup. Candidate
peak RSS is higher in four runs, so this is not evidence of a memory reduction.
All 12 pipelines passed the normal gate and emitted byte-identical sets of 156
JavaScript files. Raw GNU time outputs, commands, OTLP JSONL and manifests are in
`excalidraw-completed-index-paired/` under the task scratch root; binary
identities are in `completed-index-tools/identity.json` and
`projection-tools/identity.json`.

The [focused checks](https://app.buildbuddy.io/invocation/620871c7-9210-4faf-ae50-a19f67ba3a66)
passed the analysis library checks, analysis tests, candidate-object escape CLI
tests and whole-program value-flow CLI tests. This public regression check does
not establish the separate large-consumer completion target.

### Shared-summary queries: public Excalidraw check

[#17418](https://github.com/agentydragon/ducktape/pull/17418) intersects each
distinct retained candidate-summary bitmap once per value query. Its transient
identity set preserves the ordered union and drops at query completion. The
[optimized build](https://app.buildbuddy.io/invocation/d4dd89e4-56f0-4e91-8d2c-481716f5c9b1)
is commit `4dffb30bee12ff67ed37e0cb2efdbb95910b55bf`, based on devel
`7857e7c5ca`. Analyzer SHA-256:
`816967f0c550e55b2e0790fe3f601a446298e841b7423386d5afda3adf93104b`.
The baseline is the completed-index candidate above; intervening Debundle
changes affect tests and documentation, not production behavior.

Three adjacent pairs per metrics mode alternate execution order, using the same
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. Timings sum fresh projection, analysis and
application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 15.51 → 14.79 s                   | 23.14 → 22.24 s                  | 0.9697                   | 0.9018–1.0098      | 713,396 → 809,752                  |
| On      | 14.88 → 14.96 s                   | 22.09 → 22.41 s                  | 1.0173                   | 0.9825–1.0310      | 809,836 → 810,208                  |

Every pair stays below the 10% slowdown threshold. All 12 pipelines pass the
normal gate and emit byte-identical sets of 156 JavaScript files. RSS varies
between approximately 697 and 791 MiB across these runs; this comparison does
not establish a memory reduction. Raw GNU time outputs, commands, OTLP JSONL
and manifests are in `excalidraw-candidate-query-paired/` under the task scratch
root, with binary identities in `candidate-query-rebased-tools/identity.json`
and `completed-index-tools/identity.json`.

The rebased implementation passed required CI, including the
[ordinary-test invocation](https://app.buildbuddy.io/invocation/bb53272d-6248-5719-8999-4bcc9d795bf0).
This regression result does not establish the separate passing-subset completion
target for the large private consumer.

### Activation-local property joins: public Excalidraw check

[#17422](https://github.com/agentydragon/ducktape/pull/17422) reuses exact
property bitmap joins within one rule activation. Its bounded state and identity
argument are in <../docs/inference.md>. The
[optimized build](https://app.buildbuddy.io/invocation/443ce17b-6a4b-47dd-94d8-328ff788a08a)
is commit `bdf083778958a52eb564ed79e46f02d770a460c2`; the subsequent test-only
fix does not change these production binaries. Analyzer SHA-256:
`55f4a3b435dae4880d6a8a0805a13f60c1cc5f00ff0c243d981f031b440ac9b4`.
The baseline is the shared-summary query candidate above, whose Debundle source
matches this change's devel base before the property-join patch.

Three adjacent pairs per metrics mode use identical staged public inputs and
four workers, alternate execution order and run without another local consumer.
Metrics export every second when enabled. Timings include fresh projection,
analysis and application; compilation and input staging are excluded.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 15.26 → 14.80 s                   | 22.55 → 21.99 s                  | 0.9710                   | 0.9648–0.9908      | 808,960 → 808,840                  |
| On      | 15.01 → 14.95 s                   | 22.51 → 22.31 s                  | 0.9913                   | 0.9701–1.0240      | 714,408 → 809,008                  |

Every pair stays below the 10% slowdown threshold. All 12 pipelines pass the
normal gate and emit byte-identical sets of 156 JavaScript files. One enabled
analysis records 311,355 memo hits and 29,875 misses, confirming that the workload
exercises reuse; those work counts are not timing attribution. The RSS variation
does not establish a memory reduction. Raw GNU time outputs, commands, OTLP JSONL
and manifests are in `excalidraw-source-join-paired/`, with binary identities in
`source-join-tools/identity.json` and `candidate-query-rebased-tools/identity.json`.

[Focused validation](https://app.buildbuddy.io/invocation/373dd24e-781a-40eb-8bf8-9c9cf385358c)
passes the analysis and work-metrics libraries, their tests, and the
candidate-object escape, whole-program value-flow and metrics CLI tests. The
large private passing-subset completion target remains a separate requirement.

### Borrowed effect footprints: public Excalidraw check

[#17437](https://github.com/agentydragon/ducktape/pull/17437) queries immutable
statement read/write sets directly when recording edge premise verdicts. It
preserves the first conflict witness and array index/length alias law without
rebuilding an overlap index for each edge. The
[optimized build](https://app.buildbuddy.io/invocation/7946a301-7169-46ee-8795-a93e7466ab7b)
is commit `4dfd9dc87edd801cda92326d1b3cb5a2401aa45b`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Analyzer SHA-256:
`bf9565cc5c6ca81f90aec05178d9dcdb8a956c8f29f271f2d4df847995f42ab2`.
The baseline is the activation-local property-join build above; intervening
Debundle changes at this base affect tests and documentation only.

Three adjacent pairs per metrics mode alternate execution order, with identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. Timings include fresh projection, analysis
and application, excluding compilation and staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 13.68 → 13.09 s                   | 20.87 → 20.10 s                  | 0.9690                   | 0.9479–0.9715      | 712,640 → 713,440                  |
| On      | 13.36 → 13.41 s                   | 20.31 → 20.49 s                  | 1.0030                   | 0.9860–1.0234      | 714,624 → 713,960                  |

All 12 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. Every paired wall ratio meets the 10% slowdown guardrail.
These small measurements do not establish a general runtime or memory reduction.
Raw GNU time outputs, commands, OTLP JSONL and manifests are in
`excalidraw-borrowed-cell-conflicts-paired/` under the task scratch root; binary
identities are in `borrowed-cell-conflicts-tools/identity.json` and
`source-join-tools/identity.json`.

[Focused validation](https://app.buildbuddy.io/invocation/820666a3-9372-4945-bf54-ac6f00800dd5)
passes `:analysis` with Clippy, `:analysis_test`, both commuting-statements CLI
tests, ordered-own-data-field and foreign-call-summary tests. The small-subset
oracle compares borrowed lookups with the existing indexed witness order.
Fresh large-consumer passing-subset completion remains a separate requirement.

### Rejected cell-ID arena

The prototype `edb9bb19ad16eb2f5ffdaf017a34e77e29cda3ac` replaces repeated
solver key storage with internal cell IDs, a key interner, reverse reader vectors
and per-cell `Option<Fact>` slots. Its
[optimized validation](https://app.buildbuddy.io/invocation/9ab43516-b7af-49e6-b7bc-cd089f690376)
passes analysis Clippy, solver tests and the whole-program value-flow CLI tests.
The comparison baseline is composed candidate
`c560e141acc664c394d5db5d014efd1e37b22735`, not devel.
The only production difference in the measured candidate is the arena.

Three adjacent pairs per metrics mode alternate execution order on identical
staged public Excalidraw inputs, with four workers and no overlapping local
consumer. Metrics export every second when enabled. Timings sum fresh projection,
analysis and application; compilation and staging are excluded.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.15 → 14.97 s                   | 20.05 → 22.07 s                  | 1.1587                   | 1.1361–1.1885      | 712,116 → 1,864,028                   |
| On      | 12.93 → 15.15 s                   | 19.70 → 22.24 s                  | 1.1878                   | 1.1530–1.2019      | 713,452 → 1,914,320                   |

All 12 normal-gate pipelines pass and emit byte-identical sets of 156 JavaScript
files. Every paired wall comparison nevertheless exceeds the 10% slowdown
guardrail. Reject this representation for adoption. Reserving an inline optional
fact for every absent read is a candidate explanation, not yet measured
attribution; key/cell layout and positive/absent cardinalities must establish
that before another representation is chosen. Smaller watcher keys alone do not
guarantee a cheaper complete solver.

Raw GNU time outputs, commands, OTLP JSONL and manifests are in
`excalidraw-cell-id-arena-paired/` under the task scratch root, with binary
identities in `cell-id-arena-tools/identity.json` and
`composed-cell-tools/identity.json`. This prototype has no adoption PR and was
not used for a private acceptance attempt.

The subsequent [layout probe](https://app.buildbuddy.io/invocation/fa808387-5804-42a9-990e-b6a4f203dbbe),
commit `cd12e7abe6d46d5227875220383dd99df32a967c`, measures `FlowKey` at 12 bytes,
`FlowFact` and `Option<FlowFact>` at 56, and the actual
`InferenceCell<Judgment<FlowKey, FlowFact>>` at 96. Existing metrics count read
occurrences and new facts, not distinct absent cells, so these sizes do not yet
attribute the observed RSS increase.

Code review finds a separate lifetime regression: `take_final` removes consumed
facts, but the prototype retains both owned copies of every consumed key until
solve completion. Runtime request/result keys own `ProgramPoint` path vectors.
The old map drops those consumed keys immediately. Separating positive fact slots
therefore cannot by itself restore the old lifetime bound. Any revision must
retire unobserved completed cells safely while retaining stable IDs for cells
still referenced by watchers or pending delivery.

### Pending dirty-key membership: public Excalidraw check

[#17434](https://github.com/agentydragon/ducktape/pull/17434) deduplicates pending
per-rule keys using hash membership, then restores their sorted order once per
activation. The [optimized build](https://app.buildbuddy.io/invocation/f135bdc1-1851-4071-894e-0f849d28bc54)
is commit `9f458c0a83d6e3873e364c1fcf731cd766e7d786`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Later changes are tests and
documentation only. Analyzer SHA-256:
`de2aa01da384146adb43eda7a37f24dd1f15f1f8a782a78d3a9327af75c32ec9`.
The baseline is the activation-local property-join build above; intervening
Debundle changes before this candidate are tests and documentation only.

The same staged public inputs, four workers and three alternating adjacent pairs
per metrics mode include fresh projection, analysis and application. No other
local consumer overlaps these runs; enabled metrics export every second.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Median peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ----------------------------------------- |
| Off     | 13.31 → 13.32 s                   | 20.15 → 20.09 s                  | 1.0008                   | 0.9834–1.0131      | 713,900 → 712,836                         |
| On      | 13.70 → 13.44 s                   | 20.52 → 20.52 s                  | 0.9810                   | 0.9567–1.0122      | 713,572 → 713,712                         |

Every pair is within the 10% slowdown limit. All 12 normal gates pass and emit
identical manifests of 156 JavaScript files. These results establish no material
speedup on this corpus. Raw GNU time outputs, commands, metrics and manifests
are in `excalidraw-pending-key-paired/` under the task scratch root;
`pending-key-tools/identity.json` records the binaries.

[Focused validation](https://app.buildbuddy.io/invocation/62d34134-0e48-4382-bcd1-89c88d81362b)
passes the analysis library and tests, plus candidate-object escape,
whole-program value-flow and metrics CLI tests. The large private passing-subset
completion requirement remains open.

### Compact inference cells: public Excalidraw check

[#17447](https://github.com/agentydragon/ducktape/pull/17447) stores cell IDs in
reader memberships and pending changes, with sparse judgment slots and safe
retirement of unwatched consumed cells. Semantic keys still determine delivery
order. The lifetime and cost contract is in
[Value subscriptions](../docs/inference.md#value-subscriptions).

The [optimized candidate build and seven focused tests](https://app.buildbuddy.io/invocation/e0f5f401-66af-4204-8ef6-11cdf0cfd401)
pass at `76e5f5ae1f42403c4e9831a1de02efd662562c2e`, whose parent is
`1da38cb38c026036da83730308e0aa41b92d1b05`. The baseline is the pending-key
build above, `9f458c0a83d6e3873e364c1fcf731cd766e7d786`; the intervening
parent changes add only a test Clippy annotation and documentation. Candidate
analyzer SHA-256:
`80ca7668a1fc44ef620a0605d84d8e1bed503ee499e106f2fe1f929046c82ad7`.

Three alternating adjacent pairs per metrics mode use the same staged public
inputs, four workers, and fresh projection, analysis and application. Enabled
metrics export every second. No other local corpus runs overlap.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Median peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ----------------------------------------- |
| Off     | 13.06 → 13.24 s                   | 19.94 → 19.87 s                  | 1.0138                   | 0.9055–1.0347      | 714,300 → 707,828                         |
| On      | 13.19 → 13.15 s                   | 20.09 → 20.05 s                  | 0.9917                   | 0.9303–1.0108      | 715,448 → 708,828                         |

Every pair is within the 10% slowdown limit. All 12 normal gates pass and emit
identical manifests of 156 JavaScript files. All six metrics-enabled runs have
identical final cumulative source-inference operation totals, including 937,504
reads, 289,302 subscriptions, 722,463 wake attempts and 75,095 new facts.
This corpus establishes the regression limit, with no material speedup claim.

Raw GNU time outputs, commands, OTLP metrics and JavaScript manifests are in
`excalidraw-clean-arena-paired/` under the task scratch root. The
`pending-key-tools/identity.json` and `cell-id-arena-final-tools/identity.json`
files record build provenance and binary hashes. The focused tests include
analysis, whole-program flow, retained factories/constructors, local records,
retained-instance publication and contract-audit CLI behavior. The fresh large
private passing-subset completion requirement remains open.

### Paired heap-content value queries: public Excalidraw check

[#17436](https://github.com/agentydragon/ducktape/pull/17436) shares one exact
expression evaluation across paired heap-content classifiers. Its lifetime and
semantic contract is in <../docs/inference.md#heap-content-value-query-reuse>.
The [optimized build](https://app.buildbuddy.io/invocation/7ba49e01-bd55-42d5-9c0a-098a50b2e93a)
is commit `4e26d6c33181b645a91709a08eec3d8e2d48a67e`; analyzer SHA-256:
`acfd2d82d8e27efeecb2ade01593b9bb09006a2ef1654e9118dcabeb75320133`.
The baseline is the activation-local property-join build above. Intervening
Debundle changes in this candidate's base affect tests/documentation only.

Three adjacent pairs per metrics mode use identical staged public inputs and
four workers, alternate execution order and run without another local consumer.
Metrics export every second when enabled. Timings include fresh projection,
analysis and application; compilation and input staging are excluded.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Median peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ----------------------------------------- |
| Off     | 13.96 → 14.04 s                   | 20.91 → 20.93 s                  | 1.0113                   | 0.9713–1.0233      | 713,676 → 712,492                         |
| On      | 13.61 → 14.40 s                   | 20.49 → 21.61 s                  | 1.0218                   | 1.0155–1.0860      | 713,400 → 714,332                         |

Every pair stays below the 10% slowdown threshold. All 12 normal gates pass and
all 156 emitted JavaScript files are byte-identical. These measurements do not
establish an Excalidraw speedup or a memory reduction. Raw GNU time outputs,
commands, OTLP JSONL and manifests are in `excalidraw-heap-query-reuse-paired/`,
with binary identities in `source-join-tools/identity.json` and
`heap-query-reuse-tools/identity.json` under the task scratch root.

[Focused validation](https://app.buildbuddy.io/invocation/369e99ce-1f31-40c8-94c6-87339ea5e033)
passes the analysis library's Clippy check, analysis tests, and the unresolved
callee, foreign-call summary, candidate-object escape, program-value flow and
ordered-own-data-field CLI tests. The fresh private passing-subset completion
target remains separate.

### Transient expression dependency collection: public Excalidraw check

Expression reads now deduplicate dependency keys in a transient hash set and
freeze the result into the existing ordered dependency set once per expression.
Every underlying fact read still occurs; retained watchers, dirty-key delivery
and value/proof semantics are unchanged. The parsed-source oracle checks
duplicate reads and distinct keys against the solved ordered facts.

The [optimized build](https://app.buildbuddy.io/invocation/03c1d737-d8a2-4fa1-8f7e-82d2d03b1f8d)
is commit `8c72625720d0ec21c816e6875bfa004018e8f689`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Analyzer SHA-256:
`9905775a078cec9ef6481d3f5dc9a4921bc52fe169688627b2dfe44715bad1cc`.
The baseline is the activation-local property-join build above; intervening
Debundle changes at this base affect tests and documentation only.

Three adjacent pairs per metrics mode alternate execution order, using identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. GNU time measures fresh projection, analysis
and application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.37 → 13.13 s                   | 20.32 → 19.90 s                  | 0.9843                   | 0.9565–0.9940      | 713,300 → 808,320                     |
| On      | 13.52 → 13.38 s                   | 20.23 → 20.33 s                  | 0.9907                   | 0.9852–1.0214      | 810,036 → 809,232                     |

All 12 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. Every paired wall ratio meets the 10% slowdown guardrail.
RSS varies across these small runs; they do not establish a general runtime
or memory reduction. Raw commands, GNU time outputs, OTLP JSONL and manifests
are in `excalidraw-expression-dependencies-paired/` under the task scratch root;
binary identities are in `expression-dependencies-tools/identity.json` and
`source-join-tools/identity.json`.

[Focused tests](https://app.buildbuddy.io/invocation/d3502785-3844-48c2-a479-7300485482d3)
pass `:analysis_test` and the real `program_value_flow_test` CLI suite.
[Library validation](https://app.buildbuddy.io/invocation/e0953d2a-85c8-42b6-8fac-0158d02da970)
passes `:analysis`, including Clippy. Fresh large-consumer passing-subset
completion remains a separate requirement.

### Compact completed property keys: public Excalidraw check

The immutable completed-value index keeps sorted property keys in one flat
vector with per-site ranges and values in a parallel vector. Binary search
compares compact keys without striding over the larger value payload. Values
move into the index once and retain their existing bitmap identities. Empty
sites stay empty; no old/new duplicate property store is retained.

The [optimized build](https://app.buildbuddy.io/invocation/bcf472b9-285c-48b2-a4f0-7adcf8c8e370)
is commit `9aa4766cd33550031abe878914c26a373b5c6882`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Analyzer SHA-256:
`4862b70e2707755bc4fb95599e44efdfb089d4f502df9e6086391f83c95e83db`.
The baseline is the activation-local property-join build above; intervening
Debundle changes at this base affect tests and documentation only.

Three adjacent pairs per metrics mode alternate execution order, using identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. GNU time measures fresh projection, analysis
and application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.83 → 13.39 s                   | 21.02 → 20.19 s                  | 0.9662                   | 0.9516–0.9889      | 712,908 → 808,968                     |
| On      | 13.34 → 13.26 s                   | 20.19 → 20.15 s                  | 0.9918                   | 0.9779–1.0158      | 714,020 → 810,060                     |

All 12 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. Every paired wall ratio meets the 10% slowdown guardrail.
RSS varies across these small runs; they do not establish a general runtime
or memory reduction. Raw commands, GNU time outputs, OTLP JSONL and manifests
are in `excalidraw-property-index-paired/` under the task scratch root; binary
identities are in `property-index-tools/identity.json` and
`source-join-tools/identity.json`.

[Focused validation](https://app.buildbuddy.io/invocation/e65e2fcc-817e-4c23-8f86-b9c6ba9e1b44)
passes `:analysis` with Clippy, `:analysis_test`, `program_value_flow_test` and
`candidate_object_escape_test`. Parsed-source oracles compare every completed
key and bitmap identity against the ordered solved facts across multiple
populated sites, absent keys, empty values and Top. Fresh large-consumer
passing-subset completion remains a separate requirement.

### Observed access bits in the conflict sweep: public Excalidraw check

Effect-relocation participant streams retain the observed read/write bits from
their indexed cell. This replaces repeated statement-evidence map lookups for
each overlapping incidence. The sweep keeps its existing ordinal/unit/cursor
ordering, duplicate-conflict OR, array overlap law and premise decisions.
Allocation still participates as a writer without becoming an observed write.

The [optimized build and focused validation](https://app.buildbuddy.io/invocation/21ed338e-2551-4051-a432-682c03f5c13b)
use commit `289088ea2b15713a6dcdd4c845ab0832d0549f6e`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Debundler SHA-256:
`535f9e858a9a4ef8b6b1cccdf8a3048de9b044e7b35b4b31e736dfc19dd3d673`.
The baseline is the activation-local property-join build above; intervening
Debundle changes at this base affect tests and documentation only.

Three adjacent pairs per metrics mode alternate execution order with identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. GNU time measures fresh projection, analysis
and application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.15 → 13.23 s                   | 20.00 → 20.02 s                  | 0.9992                   | 0.9943–1.0248      | 808,240 → 714,464                     |
| On      | 13.55 → 13.20 s                   | 20.56 → 19.93 s                  | 0.9742                   | 0.9690–0.9789      | 715,352 → 715,028                     |

All 12 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. Every paired wall ratio meets the 10% slowdown guardrail.
These small runs do not establish a general speedup or memory reduction;
carrying bits increases each indexed participant's payload. Raw commands,
GNU time outputs, OTLP JSONL and manifests are in
`excalidraw-observed-bit-paired/` under the task scratch root; binary identities
are in `observed-bit-tools/identity.json` and `source-join-tools/identity.json`.

Validation passes `:gate` with Clippy, `:gate_test`,
`effect_tv_bounded_reports_test`, `effect_tv_global_provenance_test`,
`commuting_statements_test` and `commuting_statements_gate_test`.
The parsed-source oracle compares the stored bits with the previous evidence
lookup formula; full CLI rows check the same observed-conflict contract.
Fresh large-consumer passing-subset completion remains a separate requirement.

### Transient heap-content collection: public Excalidraw check

Heap-content annotation accumulates compact array/Set identities in temporary
hash sets, then freezes each source item's eager and callable effects into the
existing ordered representation. The temporary callable map is scoped to one
source item; repeated callable identities union into the durable summaries.
Native proof uses, opacity, allocation classification and async/accessor
boundaries remain unchanged.

The [optimized build](https://app.buildbuddy.io/invocation/a679f877-01c7-4064-8f7b-4360a3f2ad53)
is commit `d385d0123e2cc2133e28c6640744b80c44ce1888`, based on devel
`10d82686725183eb299baf4fc6e3cdcfb247f736`. Analyzer SHA-256:
`44c36884c9d60ad843adfde22040c4ed75dc260372e8f1c3f9731c0fa2b074d7`.
The later test-only correction does not alter these binaries. The baseline is
the activation-local property-join build above; intervening Debundle changes
at this base affect tests and documentation only.

Three adjacent pairs per metrics mode alternate execution order with identical
staged public inputs, four workers and no overlapping local consumer. Metrics
export every second when enabled. GNU time measures fresh projection, analysis
and application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.36 → 13.70 s                   | 20.44 → 20.82 s                  | 1.0140                   | 0.9785–1.0254      | 809,132 → 713,352                     |
| On      | 14.01 → 13.26 s                   | 20.95 → 20.05 s                  | 0.9767                   | 0.9247–0.9786      | 809,820 → 810,296                     |

All 12 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. Every paired wall ratio meets the 10% slowdown guardrail.
These small runs do not establish a general speedup or memory reduction.
Raw commands, GNU time outputs, OTLP JSONL and manifests are in
`excalidraw-heap-content-sets-paired/` under the task scratch root; binary
identities are in `heap-content-sets-tools/identity.json` and
`source-join-tools/identity.json`.

[Focused validation](https://app.buildbuddy.io/invocation/2a465e3f-af90-421c-9a26-50639a74cbf7)
at `5dfa2d9f6dadef823dcd13f0299ef90644b20a0c` passes `:analysis` with Clippy,
`:analysis_test`, `collections_test`, `initialization_test` and
`soundness_plain_data_escape_test`. Parsed-source controls cover eager/callable
array effects, escapes and async/class boundaries. Native Set behavior uses
the real CLI collection controls with their required source proofs.
Fresh large-consumer passing-subset completion remains a separate requirement.

### Disjoint reader/writer participant indexes: public Excalidraw check

A cell participant that both reads and writes already appears in every access's
writer query. Retain its complete evidence there and index only readers without
writes in the reader stream. This removes duplicate overlap visits while
preserving distinct-cell overlaps, pair decisions and full report evidence.
Allocation participates in writer selection without manufacturing an observed
write.

The [optimized build and focused validation](https://app.buildbuddy.io/invocation/5b63a798-ca1f-4231-9580-e7049600b98a)
use candidate `a3367f6de8669be3e7f46b3b1323fe3d84b11044`, based directly on
`4b0eaef7f1a3092c98f384db306afa30cc6bfc67`. The
[baseline optimized build](https://app.buildbuddy.io/invocation/050e2877-6b2b-489e-bef9-662dd91e9a3c)
uses that exact devel commit. Candidate debundler SHA-256:
`d8805595bfbe4c1914e774b94dd7a1665557eddb211f8a47081e2d0a6e640f32`.

Six adjacent pairs per metrics mode alternate execution order with identical
staged public inputs, four workers and no overlapping local corpus workload.
Metrics export every second when enabled. GNU time measures fresh projection,
analysis and application; compilation and staging are outside the measurement.
The first three pairs contained one +11.35% metrics-enabled wall sample, so
three more pairs were collected. All observations remain in these results.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Maximum RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ------------------------------------- |
| Off     | 13.28 → 13.51 s                   | 20.24 → 20.46 s                  | 1.0140                   | 0.9701–1.0585      | 713,892 → 809,296                     |
| On      | 13.69 → 13.69 s                   | 20.70 → 20.71 s                  | 1.0025                   | 0.9623–1.1135      | 809,724 → 809,852                     |

All 24 pipelines pass the normal gate and emit byte-identical sets of 156
JavaScript files. The repeated comparison supports no material regression
against the requested 10% guardrail; it does not establish that every individual
run stays below it, nor demonstrate a speedup. The six additional paired ratios
range from 0.9701 to 1.0411. Raw commands, GNU time outputs, OTLP JSONL and
manifests remain in `excalidraw-disjoint-cell-paired/` under the task scratch
root; binary identities are in `devel-4b0-tools/identity.json` and
`disjoint-cell-tools/identity.json`.

Focused validation passes `:gate` with Clippy, `:gate_test`,
`effect_tv_bounded_reports_test`, `effect_tv_global_provenance_test`,
`commuting_statements_test` and `commuting_statements_gate_test`. The added CLI
regression reorders two read/write initializers and checks rejection, one full
cell conflict, exact counts and both sides' read/write evidence. Fresh large
passing-subset completion remains a separate requirement.

### Retained factory carrier admission: public Excalidraw check

[#17446](https://github.com/agentydragon/ducktape/pull/17446) preserves the
current audited factory result when whole-program source flow has conservative
alternative origins. Rebind, mutation, taint and exposure refusals remain in
place; the precise exception is in <../docs/purity_soundness.md>.

The baseline is devel `4b0eaef7f1a3092c98f384db306afa30cc6bfc67`
([optimized build](https://app.buildbuddy.io/invocation/050e2877-6b2b-489e-bef9-662dd91e9a3c)).
The candidate is `f73b0b08e3084e270a306a55b2fe91e2f3849149`
([optimized build](https://app.buildbuddy.io/invocation/63e80db0-2ee7-4a0e-aa34-f7580eecee9d));
subsequent refinements change tests and documentation only. Candidate analyzer
SHA-256: `3965cede210c5cdfe7b21fe1db8ead776c809f8796c98bbf221d06e5db7ea723`.

Three adjacent pairs per metrics mode use identical staged public inputs and
four workers, alternate execution order and run without another local corpus.
Metrics export every second when enabled. Timings sum fresh projection,
analysis and application, excluding compilation and input staging.

| Metrics | Median wall, baseline → candidate | Median CPU, baseline → candidate | Median paired wall ratio | Paired ratio range | Peak RSS KiB, baseline → candidate |
| ------- | --------------------------------- | -------------------------------- | ------------------------ | ------------------ | ---------------------------------- |
| Off     | 13.58 → 13.39 s                   | 20.48 → 20.30 s                  | 0.9963                   | 0.9802–0.9993      | 712,564 → 808,924                  |
| On      | 13.51 → 13.19 s                   | 20.34 → 20.16 s                  | 0.9829                   | 0.9763–1.0478      | 810,048 → 714,304                  |

Every observed pair stays below the requested 10% slowdown threshold. All 12
pipelines pass the normal gate and emit byte-identical sets of 156 JavaScript
files. RSS varies across repetitions; this check does not establish a memory
reduction or private-consumer speedup. Raw GNU time files, commands, OTLP JSONL
and manifests are in `excalidraw-retained-origin-fix-paired/` under the task
scratch root; binary identities are in `devel-4b0-tools/identity.json` and
`retained-origin-fix-tools/identity.json`.

The [final focused checks](https://app.buildbuddy.io/invocation/20852e5f-798a-4e30-843d-d6132d87886f)
at `29c3a806c8c130ee9bd91397c692c32d96c9e783` pass analysis tests and the
whole-program value-flow, retained-callback factory and retained-constructor
CLI suites. The positive regression checks fresh/saved gate acceptance and
Node output; refusal controls preserve multiple ordinary origins, binding
reassignment and mutation/exposure restrictions. The large private passing
subset still requires its separate fresh-pipeline and emitted-runtime checks.
