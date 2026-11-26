# `DebundleAnalyze` cost: measurements and findings

Why analysis averages few cores on large inputs, where its serial time goes,
and what each measured change did. Measurement protocol:
<../docs/bazel_integration.md#public-performance-workflow>. Open tasks:
<../plans/analysis_performance.md>. Raw receipts cited as `/tmp/agents/...`
in earlier revisions of this file were local scratch; BuildBuddy invocations
and the revisions named here are the durable record.

## Remote executor allocation (private corpora)

From `bb execution get <id> --output=json` (`schedulingMetadata`,
`usageStats.timeline`) and the Firecracker VM log for five private
`DebundleAnalyze` actions:

- Every action asks for `EstimatedComputeUnits=3` (inherited from the
  `rbe_linux_x64` platform; corpus `exec_properties` set only memory/disk).
  BuildBuddy still boots a 6-vCPU VM; Rayon defaults to 6 threads and parallel
  phases run at 5–6 cores.
- The tool is `-c opt` in the exec configuration: `opt-level=3`, no debug
  assertions, 16 codegen units, no LTO.

| Input                            |   Wall | CPU s | At ~1 core | At 5–6 cores |
| -------------------------------- | -----: | ----: | ---------: | -----------: |
| Large web corpus (807 chunks)    | 2383 s |  5606 |        70% |          26% |
| Multi-chunk CLI                  |  294 s |   431 |        80% |           9% |
| 44-chunk app                     |  250 s |   417 |        73% |          15% |
| Single large chunk (desktop app) | 2824 s |  2870 |       100% |           0% |
| Single large chunk (web app)     |  580 s |   608 |        99% |           0% |

The 807-chunk timeline: 0–2 min serial, 2–14 min parallel at ~5.4 cores, then a
26-minute serial tail. Even 16 ideal cores would only reach ~1910 s; requesting
more CPU does not address the tail.

## Workloads

**Package corpus.** `@excalidraw/excalidraw` 0.18.0 production package
(`sha512-QkIiS+5qdy8lmDWTKsuy0sK/fen/LRDtbhm2lc2xcFcqhv2/zdg95bYnl+wnwwXGHo7kEmP65BSiMHE7PJ3Zpw==`)
with `react`/`react-dom` 19.3.1:

```sh
npm install --no-audit --no-fund --ignore-scripts \
  @excalidraw/excalidraw@0.18.0 react@19.3.1 react-dom@19.3.1
```

Corpus: every `.js` under `node_modules/@excalidraw/excalidraw/dist/prod`
(65 esbuild ESM chunks), listed sorted in `js-files.txt`;
`--packages-root node_modules`. Residual-baseline spec: every chunk
`inline_in_entry`, empty `logical_modules`/`chunk_renames`, no vendor swaps.

```sh
debundle_project_analysis --spec spec.json --packages-root node_modules --out proj
DUCKTAPE_DEBUNDLE_PROGRESS=1 debundle_analyze --projection proj \
  --tree-source-root . --packages-root node_modules --out out.analysis
```

React is a package import, not vendored; the run is ~100× shorter than the
private web corpus. Phases at `f4ccff2f` (from `perf` thread counts and progress
markers): 0–1 s `collect_program_summary` (74%
`resolve_imported_callable_summaries`, 42% of that serial
`prepare_callable_capabilities`); 1–3.5 s per-chunk `par_iter`; 3.5–18 s
`chunk-FX7ZIABN` alone; 18–18.6 s `write_artifact` (`rmp_serde` into
single-threaded zstd).

**App-build corpus** (`//devinfra/js/debundle/excalidraw`, 0.18.1, React and
vendors bundled). Stage with `bbr build //devinfra/js/debundle/excalidraw:{source,inputs}`;
project with `debundle_project_analysis --tree-config ... --out-root ...`. The
spec's vendor marks need `--package-root` for `clsx@1.1.1` and `roughjs@4.6.4`
(a copied analyzer has no runfiles). Two shapes:

- **Split** (current): `app/vendor/` puts roughjs and clsx in their own chunks.
- **Inlined**: roughjs inside the 545 KB shared editor chunk. Build the same
  targets in a detached worktree at `6114b352`; its vendor marks are empty, so
  drop `--package-root`.

RBE `DebundleAnalyze` at the time (opt exec configuration):

| Shape                                      | Wall    | CPU     | Peak RSS | Invocation                             |
| ------------------------------------------ | ------- | ------- | -------- | -------------------------------------- |
| Inlined                                    | 304.2 s | 337.1 s | 939 MiB  | `d40385ac-fd78-4d2f-aabd-cf08f606199b` |
| Inlined, with the spec's marks and modules | 340.8 s | 374.3 s | 933 MiB  | `8322fa51-4ee5-4b03-8604-ff0f58bfdd5e` |
| Split                                      | 43.6 s  | 64.9 s  | 988 MiB  | `127bc44a-c144-4049-9901-458854ba1a3f` |
| Split, `analysis_threads = 1`              | 48.6 s  | 50.2 s  | 808 MiB  | `7ec8041c-790d-4daa-9534-c5385cbae6ae` |

Total JS is unchanged; the editor chunk loses only 72 of 1,859 source units,
yet ~260 s of analysis disappears. Cost is superlinear in the callback pool,
not in size (§ Callback-pool cost model).

The **frozen expanded Excalidraw corpus** used for later paired checks comes
from checkpoint `4faeaf1dd2` (input-manifest SHA-256
`87f384df3d6ac9032494e2f81ade2a972bb04285eb5bbaea1d5e5ff6a28a2b9d`); every
accepted run emits the same 156 JavaScript files.

## Callback-pool cost model (inlined editor chunk)

Profiling: `perf_wrapper.sh` with `PERF_CALL_GRAPH=fp`, an analyzer built with
`--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes`
(DWARF unwinding of the stock binary recovered multi-frame stacks for only 29%
of samples); event `task-clock` (the VM has no hardware counters).

- **Hot paths.** `runtime_callbacks::dispatch` →
  `invoke_possible_callbacks_transfer` 59%, `promote_deferred_callbacks` 40%,
  `bounded_invocation_fallback` 35%, `Executor::unsettle` 26.5% (inclusive),
  with `imbl` set construction, name-keyed map lookups and `Atom` ordering as
  self cost. The private single-chunk flat profile has the same shape.
- **Where.** 162 of 4,297 owners take 93% of the chunk's time: one contiguous
  run of icon constants `X = k((0, f.jsxs)("g", {...}))`, where `f = ca(u1())`
  is esbuild's CommonJS interop wrapper around the bundled JSX runtime. Every
  `f.jsx` call is an opaque call that runs the possible-callback pool.
- **Scaling.** Passes per owner ≈ 1.2 × pooled callbacks; each pass costs
  ~6 µs per pass·callback. So **owner cost ∝ pool²**, and chunk cost ∝ (owners
  making opaque calls while the pool is large) × pool². Mean owner time is
  6.7 ms at pool 28 and 777 ms at pool 337. Inlined roughjs escapes classes and
  closures into the pool before the icons run.
- **The interop wrapper is not the cause.** Hand-inlining the factory,
  replacing `Symbol.for`, or routing all 734 JSX calls through one function left
  all 160 heavy owners pooling (276–589 s). After the chunk's first opaque call,
  refusals are everywhere: `reachability.unknown_call_seen` is sticky, so every
  later write takes the possible-setter path; `f.jsx` reads take
  `promote_possible_getter` because pooled components capture `f`. Once the
  pool exceeds `MAX_SYNCHRONOUS_CALLBACK_POOL`, any single refusal enters a
  pool-sized fallback. `intrinsics_never_written: {families: [object]}`
  changes nothing: neither path consults that premise.
- **`unsettle` re-derives state that already holds.** 396,047 calls visit
  93.4 M values; 99.8% insert no new unsettled object. All its effects are
  idempotent, but a previous-call memo hits at most 10% of `unsettle` work
  (<2% of the run), so it was not built. The useful shape is an incremental
  frontier: retain expanded values and revalidate each against its own reads
  (closure captures and `prefix_values`; object properties and accessors;
  descriptors).
- **Delta ceilings.** Unsound skip-when-no-new-input builds bound any sound
  per-owner delta: 276 s → 205 s (fallback walk and `unsettle`) → 92 s (also
  promotions) → 60 s (whole transfer), against a 20 s target. A sound delta
  gains at most 3–5×; reaching the target needs **fewer opaque events** (what a
  pool pass unsettles, and the sticky possible-setter path), which changes
  output and needs the output-changing review.

Remaining candidates, ranked:

1. One per-owner frontier over every opaque-call path (pool and fallback,
   promotion, export-surface walk, possible-getter `unsettle`); ceiling 4.6×,
   1.35× if scoped to the fallback alone.
2. Fewer opaque events (above). Making the interop wrapper analyzable gains
   nothing alone but is needed for this to reach the icons.
3. Start the largest chunk first in the per-chunk `par_iter` (results go to a
   `BTreeMap`, so output order is unchanged). Saves up to the parallel phase
   (~12 of 40 min on the private web corpus) when the biggest chunk sorts
   late; raises peak RSS. Check first with
   `--action_env=DUCKTAPE_DEBUNDLE_PROGRESS=1`.
4. Parallelize `prepare_callable_capabilities` (42% of a 5% head; a past
   parallel version cost peak RSS).
5. Parallelize the artifact write (per-chunk snapshots under Rayon,
   multi-threaded zstd with byte-stable output). 3% here.

Single-chunk inputs get no parallelism; item 3 does not help them.

## Landed changes

Every row kept analysis output byte-identical (`.analysis` bodies, or the
156-file emitted tree). The first four rows were single runs on a 4-core VM;
later rows use three to six alternating baseline/candidate pairs on the frozen
expanded corpus, four Rayon workers and GNU `time`, under a 10% paired
wall-slowdown guard. "Ratio" is the median paired wall ratio, metrics off / on.

| Change                                                                              | Where             | Result                                                                                                  |
| ----------------------------------------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------- |
| Transient `FxHashSet` visited set in `walk_callback_values`                         | —                 | Package corpus 18.2–18.6 → 13.6–13.9 s wall; `Values::insert` 39% → 1.7% of the tail                    |
| Export-surface callback memo, reused while every read world collection is `ptr_eq`  | —                 | Package corpus 10.7–11.1 → 9.7–10.1 s; the remaining 15% are walks after a world change                 |
| `PrefixValues` keyed by binding ordinal, not `(Atom, SyntaxContext)`                | base `d2f5b0f4`   | Split 34.5 → 24.3 s; inlined 347.5 → 299.1 s; `Atom::cmp` self 7.6% → 2.5%                              |
| Transient callback inputs (sorted `Vec`; stored sets insert only absent values)     | base `a6199437`   | Split 23.9 → 23.3 s; inlined 289.8 → 268.2 s; `OrdSet` construction 33.9% → 14.2%                       |
| Initializer target index                                                            | base `dc4d9e5d26` | Pipeline wall 16.81 → 15.84 s median, CPU −0.9%; not a stable speedup                                   |
| Source-flow telemetry                                                               | `77b5c6a15b`      | Enabling export adds 2.7% wall, 3.6% CPU                                                                |
| Shared proof propagation, retiring edges at `take_final`                            | `4b28321584`      | ~0.5% extra instructions (first version ~5%: kept consumed edges, copied empty dependency sets)         |
| Cause counters                                                                      | #17262            | +3.1% disabled, +3.3% enabled                                                                           |
| Bounded call-return relationships                                                   | `61ce1b518a`      | Wrapper fixture at 256 calls: reads 399,623 → 6,407, subscriptions 331,524 → 5,124. Ratio 1.003 / 1.040 |
| Numeric-key aggregate                                                               | #17359            | Ratio 0.972 / 1.009                                                                                     |
| Counted-loop numeric-read certificates                                              | #17374            | Ratio 0.989 / 1.009; metrics-off max RSS higher                                                         |
| Borrowed candidate-containment adjacency                                            | #17390            | Ratio 0.991 / 1.009; removed the private closure OOM (below)                                            |
| Shared chunk candidate projection                                                   | #17405            | Ratio 1.029 / 1.003                                                                                     |
| Completed-value index (binding/site slots, sparse property rows)                    | #17413            | Ratio 0.994 / 1.028; candidate RSS higher in four runs                                                  |
| Shared-summary value queries                                                        | #17418            | Ratio 0.970 / 1.017                                                                                     |
| Activation-local property joins                                                     | #17422            | Ratio 0.971 / 0.991                                                                                     |
| Pending dirty-key hash membership                                                   | #17434            | Ratio 1.001 / 0.981                                                                                     |
| Paired heap-content value queries                                                   | #17436            | Ratio 1.011 / 1.022                                                                                     |
| Borrowed effect footprints                                                          | #17437            | Ratio 0.969 / 1.003                                                                                     |
| Retained factory carrier admission                                                  | #17446            | Ratio 0.996 / 0.983                                                                                     |
| Compact inference cells (cell IDs, sparse slots, retiring unwatched consumed cells) | #17447            | Ratio 1.014 / 0.992                                                                                     |
| Transient expression dependency collection                                          | `8c72625720`      | Ratio 0.984 / 0.991                                                                                     |
| Compact completed property keys                                                     | `9aa4766cd3`      | Ratio 0.966 / 0.992                                                                                     |
| Observed access bits in the conflict sweep                                          | `289088ea2b`      | Ratio 0.999 / 0.974                                                                                     |
| Transient heap-content collection                                                   | `d385d0123e`      | Ratio 1.014 / 0.977                                                                                     |
| Disjoint reader/writer participant indexes                                          | `a3367f6de8`      | Ratio 1.014 / 1.003 over six pairs (one sample +11%)                                                    |

Excalidraw pipeline wall fell from ~16 s to ~13 s across the November 17 rows;
no single row establishes a speedup beyond its guard. Algorithms and bounds
live in <../docs/inference.md>.

**Rejected: cell-ID arena** (`edb9bb19ad`: interned solver keys, reverse reader
vectors, per-cell `Option<Fact>` slots). Ratio 1.159 / 1.188 and max RSS
712 → 1,864 MiB, which exceeds the guard. Layout probe: `FlowKey` 12 bytes,
`FlowFact`/`Option<FlowFact>` 56, `InferenceCell<Judgment<FlowKey, FlowFact>>`
96; metrics count read occurrences, not distinct absent cells, so the RSS cause
is unattributed. It also kept both owned copies of every consumed key until
solve completion, where `take_final` used to drop them. #17447 later landed
compact cells that retire unobserved completed cells while keeping stable IDs
for watched or pending ones.

## Integrated devel checkpoint (2026-11-17)

Optimized tools at `c943a196de` (Debundle subtree = devel `b37be1ecdc`) on
wyrm2 (32 logical CPUs, default Rayon width), frozen Excalidraw corpus and Tana
Web `78d928dca7`'s passing ownership subset, 600 s budget. Observations on a
busy shared host, not a matched comparison.

| Workload/run            | Pipeline wall | Pipeline CPU |   Peak RSS | Result                  |
| ----------------------- | ------------: | -----------: | ---------: | ----------------------- |
| Excalidraw, CPU sampled |       18.48 s |      28.22 s |  0.937 GiB | Complete                |
| Excalidraw, unsampled   |       31.87 s |      29.54 s |  0.943 GiB | Complete                |
| Tana, CPU sampled       |      508.11 s |     506.24 s | 16.320 GiB | Stopped during analysis |

Tana: 90.99 s projecting, 417.12 s analyzing, never reached Apply. Analysis self
samples: ~54% `FlowKey` fact-table lookup/update and dependency
membership/index operations, 19% Roaring subset checks (1,153 lost samples).
Excalidraw has no self symbol above ~4.1%.

Heaptrack: Excalidraw analysis peaks at 380.0 MB live heap (42.7% ordered
effects/interpreter state, 27.1% source value-flow facts and dependency
indexes, 15.2% other cross-module purity preparation). Tana's replay, stopped
after 90 s at 4.615 GB tracked heap, is 63.1% source value-flow facts and
indexes and 21.2% interpreter state; `solve_flow` set insertion (watcher and
watched-key membership) is 1.585 GB. This early profile does not attribute the
later 16.3 GiB RSS peak.

## Class provenance, property reuse and dependency storage (2026-11-17)

Matched pair, same inputs and host: baseline `c943a196de` vs candidate
`6fe546e91d` (stack #17181 → #17180 → #17179: bounded class-provenance closure,
property receiver/expression reuse, compact reverse dependency index, with the
correction preserving shared full-current RHS bitmaps rather than the rejected
per-receiver RHS-delta fanout). Single pairs, 600 s cap each
([build](https://app.buildbuddy.io/invocation/b2ab82e7-70a3-4d10-878d-8ecfde580f64)).

| Corpus / engine      | Pipeline wall | Pipeline CPU |   Peak RSS | Result             |
| -------------------- | ------------: | -----------: | ---------: | ------------------ |
| Excalidraw baseline  |       15.79 s |      24.64 s |  0.846 GiB | Complete           |
| Excalidraw candidate |       16.31 s |      25.00 s |  0.948 GiB | Complete           |
| Tana baseline        |      598.91 s |     592.16 s | 15.047 GiB | Capped in analysis |
| Tana candidate       |      599.06 s |     597.83 s | 16.519 GiB | Capped in analysis |

No aggregate runtime gain; RSS is higher on both corpora (the capped Tana runs
may be at different propagation progress). Candidate Tana self samples: Roaring
subset 18.4%, inference reads 15.6%, `FlowKey` equality 12.1%, dirty-key B-tree
insertion 11.0% (36% sample loss). Namespace equality and publication recursion
remain; changed property expressions and full-RHS joins still scan sets. Future
delta designs must count unique bitmap payloads and copy-on-write expansion,
not only logical deliveries: a property-sharing witness caught five retained
payloads where the original transfer retained one.

## Property-producer census checkpoint (2026-11-17)

The census omits watchers only for exact property keys with no possible
producer in the completed event list; missing reads still yield `Top`
(<../docs/inference.md#source-property-producer-census>). Census revision
`080abbb205` on metrics code `d23e4af589`, control `f2e676a2f6`.

Excalidraw (three interleaved cycles): wall −6.6% with metrics off, +2.0% on,
within the guard; identical emitted tree; noisy final cycle, no stable speedup.

The private 120 s probes reached different endpoints. At matched progress
(a control sample with exactly the census run's 2,542,207 fact rows; 802,723
vs 802,668 event evaluations), subscriptions are 78.3 M vs 63.7 M (−18.7%) and
reads 123.6 M vs 99.1 M. Unit peak memory at the endpoints was 11.1 vs 8.8 GiB.
This supports the subscription reduction; completed-solve memory and runtime
remain unmeasured.

## Private consumer (Tana) attribution

Probes reuse the frozen private projection, four Rayon workers, one-second
OTLP, and a 120 s / 16 GiB no-swap cap unless stated, and stop inside the
source value-flow solve. They are partial states, not completed runs; compare
counts only at matched progress.

**Source-set breadth.** At ~128 M reads, the largest origin set read is 20,430
(Excalidraw, completed: 228). Hot field groups include `stopPropagation` and
`preventDefault` (all absent reads), `current`, `target` and `children`; both
corpora's hottest event reads React input fields. The relation merges calls
into one cell per formal and one return cell per function with allocation-site
identities, and a member read watches the field on every possible receiver,
including absent fields. Unknown-contribution decisions: missing property
105.5 M, non-object receiver 46.3 M, dynamic property 6.3 M, non-function
callee 4.7 M (analysis decisions, not distinct sites).

**First origin-breadth transfers** (witnesses at the first 1,024/4,096/16,384
concrete-origin crossings, devel `98fcd64042`):

- A shared JSX props formal accumulates one object per call. The helper reuses
  that parameter for a ref, and the flow-insensitive binding writes all props
  objects into the returned element's `ref` (9,709 origins, later >18,000).
- A shared default-export wrapper (return the argument or its `default`) pools
  every call's argument into one return cell. A throttle variable ends up with
  47 callees and feeds a deep-merge formal from 483 to 17,134 origins. A
  capture-specific ablation of that return kept the formal under 1,024 and cut
  subscriptions 73.1 M → 59.9 M; bounded call-return relationships generalize
  it.
- A generic dynamic-key property lookup on a shared formal spreads 17,040
  origins through its shared return into unrelated helpers.

A narrower return summary must prove eligibility and keep conservative
unknowns; changing queue order, dropping origins or naming a library is not
such a proof.

**After bounded returns** (`5b27ed995c`): one ref-field correction or one
counted-loop numeric read each removed only their first broadening; the next
witness was `items[items.length - 1]` over the same pooled named fields. The
actionable distinction is numeric versus arbitrary computed keys. It never
justifies suppressing callbacks, assuming arrays, skipping getter/coercion
effects, or treating a bare loop counter as numeric without a reaching-value
proof. A late heaptrack capture (OOM at 522.9 s) peaked at 12.04 GB tracked
heap: hash-table capacity 3.98 GB, B-tree insertion 2.18 GB (~772 MB cached
expression dependencies), vector growth 1.83 GB, bitmap clones only 67 MB. The
storage target is the dependency/fact relation.

**Completion attempts** (fresh projection, 600 s / 16 GiB, metrics off):

| Engine                             | Projection | Stopped                                                            |
| ---------------------------------- | ---------: | ------------------------------------------------------------------ |
| Bounded call-return (`563b3e6455`) |    11.64 s | OOM in analysis, 472.7 s                                           |
| Numeric-key aggregate              |    15.14 s | OOM in analysis, 475.6 s                                           |
| Counted-loop certificates          |    14.44 s | OOM in analysis, 467.8 s                                           |
| Devel `5b9b7e54e5`                 |    14.26 s | OOM in candidate closure, 516.1 s                                  |
| Borrowed adjacency (#17390)        |          — | Timeout at 600 s, 12.1 GiB; closure done, `program_value_flow` not |
| Shared projection (#17405)         |          — | Timeout at 600 s, 12.1 GiB; main-chunk analysis started            |

The closure OOM: `close_program_value_flow` expanded compressed origin bitmaps
into a `DiGraphMap` (`GraphMap<FlowKey>::add_edge` 72% self in the last 30 s).
SCC closure bounds propagation steps, not that representation; #17390 traverses
the immutable compressed adjacency instead. The next stop was chunk candidate
projection (`candidate_bits` 30%, `Arc<RoaringBitmap>::make_mut` 25%, Roaring
insert 23%): every foreign allocation re-projected an identical shared summary.
#17405 projects each retained summary identity once per module. Prefix profiles
throughout put 26–36% self CPU in Roaring `Store::is_subset` and 6–8% in
dependency B-tree insertion.
