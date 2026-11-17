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
takes the output-changing review. Design: <../plans/analysis_performance.md>
§ Fewer opaque events.

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
| Largest observed origin set read |                   228 |                 20,385 |

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

Follow-up is to trace the first broadening transfers and establish which absent
property keys can have producers, then measure a bounded change against the
same inputs. Do not infer an algorithmic remedy or a completed private-consumer
run from these partial counts. The task's raw commands, metadata, count
snapshots and OTLP files are under
`/tmp/agents/2026_11_17/tana-flow-cause/{excal-attribution,tana-attribution-2}`;
these local receipts can expire and contain private source identities.

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

Raw commands, GNU time files, OTLP, manifests and tool identities are in
`/tmp/agents/2026_11_16/debundle-performance-workflow/.git/task-scratch/metrics/`:
`census-{control,census}-{off,on}-excalidraw-{1,2,3}`,
`census-control-tana-2`, `census-candidate-tana`, and the two
`census-*-comparison.json` receipts. These temporary files can expire.
