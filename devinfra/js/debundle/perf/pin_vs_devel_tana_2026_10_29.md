# Pin vs devel on the canonical web corpus (2026-10-29)

Attribution of the step 0 regression recorded in
<abstract_interpreter_step0_baseline.md> § Canonical inputs: the private
consumer's web corpus (one 9,709-unit chunk plus a vendor provider chunk) went
from accepted in 255 s / 3.67 GiB on the pinned engine to rejected in 1,121 s /
5.13 GiB on the measured devel head, with 45% more edges. No engine, spec or
fixture changed for this note. Engines: pinned ducktape
`ecb4a7344282e30257a8d96ca6a418a7a64e3ead` and devel head
`4377a57bae156d0fd2d3ac363a0ad0d71ff6de10` (115 engine commits apart, 617
repository commits).

## Method

Both engines are the exact `-c opt` executables the step 0 RBE actions ran,
fetched from the CAS by walking each `DebundlePipeline` action's input root
(invocation [d9ed8ddb](https://app.buildbuddy.io/invocation/d9ed8ddb-4889-4ff2-b862-6bb014ff543a)
execution `75024c06…/433` for the pin, [b9e9ab47](https://app.buildbuddy.io/invocation/b9e9ab47-23d9-46a2-9bf5-d22a1e119f48)
execution `6aa90ac6…/433` for devel; both actions share command digest
`994930f5…/7863`). SHA-256 of the executables: pin
`9365d228c0ff5b29730bb6c95c39eb320637eb33a0cb6544d188e52a79e78b29`
(52,642,504 bytes), devel
`19e74bb03dbd25b622369f3a42b8d8085fdc40511a0f3a960a96f16530d8d1d0`
(62,833,560 bytes); these are the `provenance.engine_sha256` values in the step 0
graphs.

Inputs were pre-staged under `/var/cache/agentydragon/debundle-perf-pin-vs-devel/`
(spec tree, snapshot, extracted file list, the esbuilt vendor bundle whose
SHA-256 matches the devel graph's input fingerprint, and the 58 `--package-root`
directories, each checked against the package versions in the RBE command). The
argv is the RBE action's argv with paths rewritten; every run copies the spec
tree afresh and writes to its own output root.

Execution mode for every row below: the engine run directly on this host
(32 logical CPUs, about 58 GiB available, 1-minute load 6–8 from other agents'
work, no Bazel) under GNU time 1.10 `-v`, with `DUCKTAPE_DEBUNDLE_PROGRESS=1` so
the engine's `[debundle progress]` markers reach stderr, which a wrapper
timestamps (seconds since exec) and annotates with the engine's `VmRSS`/`VmHWM`
read from `/proc` at each marker and once per second. Each engine ran twice:
an unprofiled control run and a run under `perf_wrapper.sh` (`cycles:u`, 49 Hz,
DWARF call chains, no lost samples; post-processing timeout raised to 7,200 s).
The two pin runs and the two devel runs overlapped in time (one profiled run
at a time: two concurrent unprivileged `perf record` sessions exhaust
`perf_event_mlock_kb`). Both engines were measured the same way. Wall times
here are not comparable with the RBE executor rows in the step 0 note (this
host runs the pin 2.9× and devel 2.5× faster than that executor); the
pin-to-devel ratio is.

The local devel run reproduces the RBE devel output: 3,146,382 edges with the
same per-kind counts and the same 5 atomic conflicts (210, 7, 9, 2, 3 members).
The local pin graph has the RBE pin's 2,163,669 edges with the same per-kind
counts. Both engines rebuild the graph deterministically, so the graphs below
can be compared unit by unit.

## Totals

| Engine | Run      | Verdict  | Wall (s) | User CPU (s) | Peak RSS (GiB) |
| ------ | -------- | -------- | -------: | -----------: | -------------: |
| pin    | control  | accepted |     87.5 |         87.9 |           3.24 |
| pin    | profiled | accepted |     81.2 |         78.8 |           3.38 |
| devel  | control  | rejected |    444.5 |        439.9 |           4.98 |
| devel  | profiled | rejected |    468.4 |        449.0 |           4.97 |

Pin to devel on the control runs: 5.1× wall, 1.54× peak RSS (RBE: 4.4× and
1.40×). The profiled-versus-control spread (7% pin, 5% devel) is the noise
floor for single runs on this loaded host; stage differences below are 5–8×,
far outside it.

## Stage attribution

Stages are the engine's progress markers. `startup` is parse, spec compilation
and selector preparation before the first marker; the gap before
`prepare_cross_module_purity` is selector projection follow-up plus, on devel,
a synchronous-execution pass that did not exist on the pin; `after last marker`
is the gate, lowering, emission and report writing (the pin emits 2,856 files
there; devel stops at the gate and writes three report files). RSS columns are
`VmRSS` / `VmHWM` at the stage's end marker; the final peak lands after the last
marker on both engines (pin at +2 s, in emission; devel at +4 s, at the
atomic-conflict report).

Control runs:

| Stage                                        | Pin wall (s) | Devel wall (s) |  Δ (s) | Pin HWM@end (GiB) | Devel HWM@end (GiB) |
| -------------------------------------------- | -----------: | -------------: | -----: | ----------------: | ------------------: |
| startup (to first marker)                    |          1.7 |            1.2 |   -0.5 |              0.70 |                0.70 |
| analyze/collect chunk structural facts       |          1.1 |            0.2 |   -0.9 |              0.98 |                0.80 |
| `project_selectors`                          |          3.0 |            2.0 |   -1.0 |              1.29 |                1.20 |
| gap before cross-module purity               |          1.2 |            6.5 |   +5.3 |              1.31 |                1.84 |
| `prepare_cross_module_purity` (vendor chunk) |         31.0 |          249.2 | +218.2 |              2.40 |                3.21 |
| gap before `compute_chunk_analysis`          |          1.1 |            0.5 |   -0.6 |              2.40 |                3.21 |
| `compute_chunk_analysis` (main chunk)        |         29.3 |          179.7 | +150.4 |              2.40 |                3.60 |
| after last marker (gate, lowering, emit)     |         18.7 |            4.9 |  -13.8 |       3.24 (peak) |         4.98 (peak) |
| total                                        |         87.5 |          444.5 | +357.0 |                   |                     |

The profiled runs give the same picture (pin 25.8 / 27.1 / 18.1 s, devel
247.9 / 195.0 / 5.5 s for the three large stages).

Two stages carry the whole regression, and neither is downstream of the
dependency graph:

- `prepare_cross_module_purity` runs on the vendor provider chunk, whose
  graph is never built or reported; its +218 s (8.0×) is independent of the
  main chunk's edge count.
- `compute_chunk_analysis` builds the main chunk's facts and its dependency
  graph; its +150 s (6.1×) is almost entirely the synchronous-execution
  interpreter (below), which runs before edges exist. Graph construction
  itself, including the new heap-content ordering, is 0.4% of devel samples.
- Everything that is superlinear in edges, the module quotient, SCC and
  realizability analysis, and graph serialization, is under 1% of samples on
  both engines and the devel run spends 4.9 s after the last marker against the
  pin's 18.7 s (the pin emits modules, devel does not).

Peak RSS does track the graph in part: +0.8 GiB is already present at the end
of the purity stage and +1.2 GiB at the end of chunk analysis (analysis state),
and the final +1.7 GiB peak arrives when the 1.0 GB `dependency_graph.json`
(688 MB on the pin) and the quotient over 347,754 module edges (206,450 on the
pin) are built.

## Profiles

`perf_wrapper.sh` reports are kept beside each run under
`/var/cache/agentydragon/debundle-perf-pin-vs-devel/runs/perf-{pin,devel}/profile/`
(`perf_record_stderr.txt`, `perf_report_flat_symbols.txt`,
`perf_report_children.txt`, `perf_report_no_children.txt`, stack slices,
`command.sh`). Pin: 9,442 samples; devel: 24,995 samples (engine comm only).
Sample counts are summed over threads: the pin's emission stage is
multi-threaded (5,654 samples in an 18 s stage), so its sample share
over-weights that stage relative to wall; the analysis stages are
single-threaded on both engines and their samples are wall time.

Self cost, top 30 (pin, `perf_report_flat_symbols.txt`):

| Share | Symbol                                                         |
| ----: | -------------------------------------------------------------- |
| 16.4% | `__memcmp_evex_movbe`                                          |
|  9.7% | `<swc_atoms::Atom as Ord>::cmp`                                |
|  6.2% | `_int_free_chunk`                                              |
|  5.8% | `__libc_malloc2`                                               |
|  4.9% | B-tree node insertion keyed by `value_flow::FlowDependency`    |
|  3.4% | `ChunkValueFlow::append_value_expr_tracking`                   |
|  3.2% | `<hstr::Atom>::as_str`                                         |
|  3.1% | `value_flow::FlowSolver::run_event`                            |
|  1.9% | `BTreeMap<FlowDependency, …>::VacantEntry::insert`             |
|  1.8% | `value_flow::Values::union_with`                               |
|  1.8% | `interpreter::Executor::promote_lazy_effects`                  |
|  1.6% | `malloc_consolidate`                                           |
|  1.4% | `value_flow::FlowSolver::merge_property`                       |
|  1.4% | `unlink_chunk`                                                 |
|  1.4% | `__memmove_avx512_unaligned_erms`                              |
|  1.4% | `_int_malloc`                                                  |
|  1.1% | `realloc`                                                      |
|  1.1% | kernel (unresolved)                                            |
|  1.0% | `BTreeMap<FlowDependency, …>` lookup                           |
|  0.9% | `sip::Hasher::write`                                           |
|  0.9% | `artifact::metrics::DirectionalDirectoryAccumulator::add_fact` |
|  0.9% | `FlatMap<BTreeSet<(Atom, SyntaxContext)>::Iter, …>::next`      |
|  0.9% | `SourceUnitReadClosure::extend_synchronous_source_units`       |
|  0.7% | `artifact::metrics::write_tree_reports`                        |
|  0.7% | `graph::build::promote_at_init_calls`                          |
|  0.7% | `BTreeMap<(Atom, SyntaxContext), …>::Iter::next`               |
|  0.6% | `BTreeMap::clone_subtree::<(Atom, SyntaxContext), …>`          |
|  0.6% | `malloc`                                                       |
|  0.6% | `cfree`                                                        |
|  0.6% | `ChunkValueFlow::candidate_object_index` closure               |

Self cost, top 30 (devel):

| Share | Symbol                                                                                |
| ----: | ------------------------------------------------------------------------------------- |
| 17.9% | `__memcmp_evex_movbe`                                                                 |
|  8.1% | `<swc_atoms::Atom as Ord>::cmp`                                                       |
|  6.7% | B-tree node insertion keyed by `value_flow::FlowRule`                                 |
|  5.8% | B-tree node insertion keyed by `value_flow::FlowKey`                                  |
|  5.1% | `<value_flow::FlowRules as IterativeRules>::merge`                                    |
|  4.9% | `interpreter::Executor::callback_values`                                              |
|  4.3% | B-tree lookup keyed by `value_flow::FlowKey` (immutable)                              |
|  3.9% | `drop_glue::<sync_effects::value_model::AbstractValue>`                               |
|  3.3% | `archery … make_mut::<imbl::nodes::btree::Branch<AbstractValue, …>>` (persistent set) |
|  3.2% | `<hstr::Atom>::as_str`                                                                |
|  2.9% | `_int_free_chunk`                                                                     |
|  2.4% | `archery … make_mut::<imbl::nodes::btree::Leaf<AbstractValue, …>>`                    |
|  2.0% | `value_flow::FlowRules::propose_candidate_values`                                     |
|  1.9% | B-tree node insertion keyed by `value_flow::FlowKey` (second instantiation)           |
|  1.6% | `interpreter::Executor::promote_lazy_effects`                                         |
|  1.4% | `value_flow::append_value_expr::<FlowRules::values_of_expr_tracking…>`                |
|  1.4% | kernel (unresolved)                                                                   |
|  1.3% | `__libc_malloc2`                                                                      |
|  1.3% | `IterativeInference<value_flow::FlowRules>::solve_values`                             |
|  0.8% | `BTreeMap<FlowKey, …>::VacantEntry::insert`                                           |
|  0.8% | B-tree lookup keyed by `value_flow::FlowRule` (immutable)                             |
|  0.8% | `value_model::Values::insert`                                                         |
|  0.7% | `imbl::nodes::btree::Branch<AbstractValue, …>::insert`                                |
|  0.7% | `SourceUnitReadClosure::extend_synchronous_source_units`                              |
|  0.7% | `__memmove_avx512_unaligned_erms`                                                     |
|  0.6% | `analysis_inputs::ProofDependencies::extend`                                          |
|  0.5% | `BTreeMap<interpreter::PrefixNeededKey, …>::Iter::next`                               |
|  0.5% | `_int_malloc`                                                                         |
|  0.5% | `malloc_consolidate`                                                                  |
|  0.5% | `_int_free_chunk` (second instantiation)                                              |

The pin's hot keys are `FlowDependency` B-trees and `(Atom, SyntaxContext)`
ordering; devel's are the shared iterative-inference `FlowKey`/`FlowRule`
B-trees, `FlowRules::merge`, and the `imbl` persistent `AbstractValue` sets of
the synchronous interpreter (`make_mut` copy-on-write and `drop_glue`), which
did not exist on the pin. `memcmp`/`Atom::cmp` stay the top two on both: they
are the comparison cost of those ordered containers, not a stage of their own.

### Self cost by symbol family

Each sample is attributed to the first frame from the leaf upward that
belongs to a family (so `memcmp` under a value-flow frame counts as value
flow). Samples whose recorded stack is empty or has no engine frame are
"unattributed": 34% of pin samples (almost all in the multi-threaded emission
stage) and 21% of devel samples (in the two analysis stages, where the 8 KiB
DWARF stack copy is too short for the deep inference recursion; their leaves
are B-tree, `memcmp` and allocator symbols like the attributed ones).

| Family                                  | Pin samples | Pin s | Devel samples | Devel s |  Δ s |
| --------------------------------------- | ----------: | ----: | ------------: | ------: | ---: |
| value flow (`facts::value_flow`)        |       1,212 |  24.7 |         8,384 |   171.1 | +146 |
| synchronous execution interpreter       |       1,105 |  22.6 |         8,077 |   164.8 | +142 |
| unattributed (empty/engine-less stacks) |       4,379 |  89.4 |         5,824 |   118.9 |  +29 |
| parse / spec (multi-threaded startup)   |         427 |   8.7 |         1,193 |    24.3 |  +16 |
| facts / other analysis                  |         384 |   7.8 |           663 |    13.5 |   +6 |
| graph build                             |         122 |   2.5 |           178 |     3.6 |   +1 |
| selector resolution                     |         120 |   2.4 |           162 |     3.3 |   +1 |
| shared iterative inference (own frames) |           0 |   0.0 |             7 |     0.1 |    0 |
| quotient / realizability / gate         |          61 |   1.2 |            39 |     0.8 |    0 |
| purity (classifier, cross-module glue)  |         119 |   2.4 |            54 |     1.1 |   -1 |
| S-chain / dataflow ordering             |           1 |   0.0 |             0 |     0.0 |    0 |
| lowering / emission                     |         662 |  13.5 |           187 |     3.8 |  -10 |
| serialization / reporting               |         814 |  16.6 |           195 |     4.0 |  -13 |

Seconds are samples ÷ 49 Hz summed over threads. Per stage: the purity stage is
69% value-flow frames on devel (91% on the pin, where it is about 10× smaller) with
the rest unattributed B-tree/`memcmp` leaves; the chunk-analysis stage is 81%
synchronous-interpreter frames on both engines.

Inclusive cost of the stage entry points (children report, share of all
samples; the pin's `materialize_logical_modules` share includes its
multi-threaded emission):

| Symbol                                                             |   Pin | Devel |
| ------------------------------------------------------------------ | ----: | ----: |
| `cross_module_purity::resolve_imported_purities`                   | 33.5% | 38.1% |
| `ChunkValueFlow::analyze` (devel: `…_with_native_set_allocations`) | 31.2% | 37.9% |
| `IterativeInference<FlowRules>::solve_values`                      |     – | 37.8% |
| `facts::analyze::complete_chunk_structural_with_inputs`            |     – | 38.1% |
| `RuntimeRules::transfer` (devel's interpreter driver)              |     – | 33.3% |
| `Executor::execute_call` (+`_transfer`)                            | 27.2% | 25.5% |
| `Executor::callback_values`                                        |  0.8% | 16.7% |
| `Executor::promote_deferred_callbacks`                             |  6.5% | 11.9% |
| `Executor::invoke_possible_callbacks`                              |  7.2% | 10.8% |
| `Executor::promote_lazy_effects`                                   | 16.2% | 10.8% |
| `Executor::bounded_invocation_fallback`                            |  1.2% |  6.9% |
| `graph::build::*` (dependency graph construction)                  |  1.5% |  0.4% |
| `graph::build::promote_at_init_calls`                              |  1.3% |  0.3% |
| `emit_heap_content_order` / `emit_cell_order_events`               |     – |  0.0% |
| realizability / module quotient                                    |  0.4% |  0.1% |
| `serde_json` serialization                                         |  0.5% |  0.1% |
| `artifact::metrics::write_tree_reports`                            |  3.3% |     – |

`callback_values` (0.8% → 16.7%) and `bounded_invocation_fallback`
(1.2% → 6.9%) are the interpreter's largest relative growers; both sit under
`promote_deferred_callbacks` / `invoke_possible_callbacks`, i.e. the
devel interpreter spends its time enumerating and invoking candidate
callbacks.

### Vendor-chunk value flow (`prepare_cross_module_purity`)

The provider chunk is prepared once (one marker pair, one `ModulePurityFacts::prepare`
call per provider module that has any required export): `prepare` runs
`complete_chunk_structural_with_inputs` over the whole provider body (the full
value-flow fixpoint) and then `prepare_synchronous_call_index_with_source_facts`,
whatever the number of exports the importer needs and however many importers
there are. So the +218 s is one whole-chunk value-flow solve of the vendor
provider, not a per-export or per-importer re-solve; the main chunk's own
value flow (in `startup`/structural facts) is small on both engines. Inclusive
cost of the devel solve, share of all samples:

| Path under `IterativeInference<FlowRules>::solve_values` (37.8%) | Devel |
| ---------------------------------------------------------------- | ----: |
| `FlowRules::evaluate` (rule bodies)                              | 19.4% |
| ↳ `FlowRules::propose_candidate_values`                          | 13.0% |
| `BTreeSet<FlowRule>::insert` (dependents of a key)               |  9.0% |
| B-tree node insert keyed by `FlowRule`                           |  8.3% |
| `IterativeContext<FlowRules> as FactContext` (reads, scheduling) |  7.9% |
| B-tree node insert keyed by `FlowKey` → `BTreeSet<FlowRule>`     |  6.1% |
| `FlowRules::merge` (join of value sets)                          |  5.5% |
| B-tree search keyed by `FlowKey` → `Judgment` (fact lookup)      |  5.4% |
| `append_value_expr` / `values_of_expr_tracking`                  |  4.6% |
| `BTreeSet<FlowKey>::insert` (reads of a rule)                    |  3.2% |

The pin's solver (`FlowSolver::run_event`, 29.7% of a 3.4× smaller sample
set) keyed its work by `FlowDependency` with `append_value_expr_tracking`
(12.8%) and `merge_property` (3.5%) as the only other hot paths. On devel the
cost is the dependency bookkeeping of the generic worklist: every rule
evaluation records its reads as `FlowKey` entries and re-registers itself as
a dependent `FlowRule` of each key, both in ordered B-trees compared with
`memcmp`/`Atom::cmp`; `propose_candidate_values` is the one rule body that
dominates. `#12792` (values-only solve) and `#12799` (separate reachability
producers) already removed part of this: the bisect below shows the solve
took more than 3 h before them.

## Edge growth

Per-kind edge counts (both graphs, same 9,709 units):

| Kind              |       Pin |     Devel |        Δ |
| ----------------- | --------: | --------: | -------: |
| `eager_use`       | 1,109,226 | 1,605,183 | +495,957 |
| `sequenced`       | 1,033,882 | 1,520,638 | +486,756 |
| `lazy_use`        |    19,612 |    19,612 |        0 |
| `hoisted_use`     |       525 |       525 |        0 |
| `local_effect`    |       317 |       317 |        0 |
| `lazy_rebind`     |        96 |        96 |        0 |
| `deferred_rebind` |         9 |         9 |        0 |
| `eager_rebind`    |         2 |         2 |        0 |

Every added `eager_use` edge carries `role.kind = promoted_at_init` (direct
eager reads: 4,226 → 4,368; promoted: 1,105,000 → 1,600,815); every
`eager_use` and `sequenced` edge on both engines has `constrains_init_order =
true`. Pair-wise, devel adds 509,653 `eager_use` pairs and drops 14,777;
adds 501,386 `sequenced` pairs and drops 14,620. Edge growth by source → target
statement kind: `var_decl → var_decl` +373k eager / +363k sequenced,
`var_decl → class_decl` +89k / +85k, `side_effect → var_decl` +27k / +27k; 86% of
the new eager pairs target a unit classified pure on both engines, so the new
edges are not purity flips of the targets.

The growth is concentrated in sources: 1,118 units gained `eager_use`
out-degree, and 331 units gained more than 500 each. Of those 331:

- 286 had complete synchronous execution on the pin and incomplete on devel
  (45 were incomplete on both); the new precision losses are
  `callback_pool_limit` (297 units), `unsupported_operation: construction`
  (192), `unsupported_operation: member_read` (74), `unknown_call_target` (31).
- 309 are `var_decl` statements, 22 `side_effect`; 233 were and stayed
  `not_pure`, 71 flipped `pure → not_pure` (rule `unknown_member`), 27 stayed
  `pure`. The chunk-wide purity flips are 119 `pure → not_pure` (113
  `unknown_member`) and 1 the other way; incomplete-synchronous-execution
  units rose from 925 to 1,306 while total precision losses fell from 11,917
  to 10,479 (`unknown_call_target` −1,729, `callback_pool_limit` +471,
  `invocation_expansion_limit` +115).
- Their new eager out-degree clusters at about 500, 800–1,000, 1,300 and
  2,600–2,700 edges, each covering 28–51% (median 39%) of all declarations
  before the statement; their `sequenced` out-degree is almost the same number
  (the heap-frontier barrier of an incomplete statement targets the live
  heap frontier). The late-chunk group (ordinals 9,200–9,671) are
  `Object.defineProperty`-style alias declarations, `new Map()`/`new Set()`
  initializers and spread array literals; the mid-chunk group (ordinals
  2,600–5,500) are declarations initialized by a local factory call with an
  object-literal argument.

So the +45% edges are the at-init promotion closure of 331 statements whose
synchronous execution devel can no longer complete, mostly because the
deferred-callback pool fills (`callback_pool_limit`) or an implicit class
construction is rejected (`construction`). That closure also forms the
1,833-module cyclic SCC (the pin's largest was 45) and the 210-unit atomic
conflict. Wall growth does not track these edges (§ Stage attribution); peak
RSS partially does.

### The 331 barrier statements

Loss reasons that appeared on devel for the 331 units with more than 500 new
promoted eager edges (each unit may carry several): `callback_pool_limit` 297,
`unsupported_operation: construction` 192, `member_read` 74,
`unknown_call_target` 31, `array_spread` 3, `membership_protocol` 1. Overlap:
192 have both `callback_pool_limit` and `construction`, 105 have
`callback_pool_limit` without `construction`, 34 have neither (their
synchronous execution was already incomplete on the pin; their closure grew
because the units they read grew). By statement shape: 132 are declarations
initialized by one local record-factory call with an object-literal argument
(the `construction` group, ordinals 2,629–5,600), 71 are
`Object.defineProperty` / `Object.getOwnPropertyDescriptor` alias chains
(ordinals 2,800 and 9,200–9,671, `callback_pool_limit` + `member_read`), 20
are `new Map()` / `new Set()` registries (`callback_pool_limit` +
`unknown_call_target`), 6 are `Object.freeze(...)` records, 102 other.
Representative ordinals: 2629, 2695, 2706 (factory calls); 9203, 9216, 9217
(alias chains); 3743, 3917, 4683 (registries).

The budgets are unchanged between the engines:
`MAX_SYNCHRONOUS_CALLBACK_POOL = 64`, `MAX_SYNCHRONOUS_INVOCATIONS = 256`,
`MAX_SYNCHRONOUS_INVOCATION_DEPTH = 64` in `facts/sync_effects/interpreter.rs`
on both the pin and devel. `callback_pool_limit` fires in the callback-call
path when the pool of possible callbacks at a call exceeds 64 entries; the
pin completed the same 286 statements under the same limit, so devel is
collecting larger pools (deferred-callback inference from stable factories,
`FinalizationRegistry`, retained arrays and constructor fallbacks: #12517,
#12582, #12656, #12819, #12849), not hitting an older limit sooner. The
report records only the limit, not the pool size reached, so how far the
limit would have to move is not measurable from the output; the source
comment at the constant warns that handing every callback the whole pool is a
quadratic value cross product, which is the cost of lifting it.
`construction` fires when `new` of a local class cannot be modelled:
`construct_local_class_transfer` returns nothing when the class has no
eligible local constructor for the arity, when a constructor
property-read argument is not an untainted plain-data object, or when the
class's construction certificate (#12631, #12633) fails; the record-factory
statements take the `new` inside the factory body with the object literal as
the argument.

What would remove the promoted edges: for the 105 pool-only units, completing
the callback enumeration (a larger pool, or fewer candidate callbacks per
call) would restore completeness and drop their promoted closures; for the
192 pool+construction units the budget alone does nothing, the construction
shape must be modelled (or asserted by the spec author via the existing
`chunk_export_purity` callback-factory / no-reentry assertions on the
factory) as well. Since these closures are what forms the 1,833-module SCC
and the 210-unit conflict, fixing both groups is what flips the gate back;
the remaining 34 were incomplete on the pin too and did not block acceptance
there. The cost of either change was not measured here (no engine edit); a
cheap first step is to record the pool size in the `callback_pool_limit` loss
so the next run says how far over 64 the pools are.

## Candidate causes, ranked

Commit ranges are in `git log ecb4a734..4377a57bae -- devinfra/js/debundle`
(115 engine commits, 2026-10-27 to 2026-10-29; 67 touch
`facts/sync_effects`, 47 `facts/`, 18 `graph/`, 17 `purity/`, 10
`inference/`). Attribution below ties each cause to a stage, a profile
family and the commits that introduced the mechanism; none is bisected.

1. **Shared iterative inference as the value-flow solver (purity stage,
   +218 s, +0.8 GiB).** `ChunkValueFlow::analyze` now runs through
   `IterativeInference<FlowRules>::solve_values` with `FlowKey`/`FlowRule`
   B-trees and `FlowRules::merge`; these symbols are 25% of devel's self cost
   and absent on the pin. The vendor provider chunk, whose facts are prepared
   only to resolve imported purities, pays 8× for it. Commits: the migration
   `39daf9f536` #12705 → `2dc4cc3768` #12762 (notably `b5e7b4ae83` #12719
   "Move value-flow facts into shared iterative inference", `1346871159`
   #12730, `eee22946b6` #12725, `5b8a886105` #12727, `b688675771` #12728), with
   the performance follow-ups `4ed437fff9` #12792 and `13de00c36e` #12799
   already included in the measured head (the latter reports an 8.6× replay
   speed-up against its parent, so this family was slower still mid-range).
   The 2026-10-29 native-input batch (`9396fb4a25` #12839, `002443226f`
   #12842, `b797753e91` #12828, `f078087076` #12855) adds more rules to the
   same solver.
2. **Deferred-callback enumeration in the synchronous interpreter (chunk
   analysis, +150 s, +1.2 GiB; and the source of the edge growth).** The
   bisect splits this cause: the edge flood is in place by `e78dd6eeb6`
   (#12685), while 123 of the 150 s arrive with the 2026-10-29 batch
   (#12828–#12878) on top of the same graph.
   `callback_values`, `promote_deferred_callbacks`, `invoke_possible_callbacks`
   and `bounded_invocation_fallback` grew from 0.8 / 6.5 / 7.2 / 1.2% to
   16.7 / 11.9 / 10.8 / 6.9% inclusive, the
   interpreter runs on `imbl` persistent sets (`make_mut` + `drop_glue` 10% of
   self cost), and 297 statements now stop at `callback_pool_limit`. Commits:
   deferred-callback inference `c670fa58b2` #12517, `acc92323aa` #12582,
   `610a33a9e6` #12656, `f22611639b` #12819, `13e50712f7` #12849; interpreter
   state and driver `73152a5d6b` #12748 (persistent collections),
   `557118f88b` #12751, `2dc4cc3768` #12762 (`RuntimeRules::transfer`),
   `d60a0ccc78` #12806 (native prefix observations at runtime program points).
3. **Native-premise validation of construction (`construction` losses on 192
   growers; part of cause 2's edge growth, small wall).** `e46b12b729`
   #12631, `1dca8a74e5` #12633, `82543a68d2` #12662, `3701389bc1` #12672,
   `d838109100` #12646: a `new` whose native premises cannot be discharged is
   now an unsupported operation, so the statement's at-init closure is
   promoted wholesale.
4. **Heap-content ordering (`sequenced` growth, no measurable wall).**
   `2c787eb3fa` #12598 introduced `emit_heap_content_order`; `a7175d9029`
   #12860, `8b8d19aa83` #12845, `894c5237b4` #12847 refined it. An incomplete
   statement is a `HeapFrontier` barrier that targets every live allocation,
   last writer and reader, which is why the 331 growers' `sequenced`
   out-degree mirrors their eager out-degree. It costs 0.02% of samples; its
   effect is on the graph's size and on peak RSS, not on wall.
5. **Pre-purity synchronous pass (+5 s).** The 6.5 s gap before the purity
   stage is `prototype_properties::contained_root*` and interpreter frames that
   the pin does not run there; `5d76e8f907` #12835 "defer structural value flow
   until after selectors" and `d60a0ccc78` #12806 moved work into this slot.
   Minor.

Not a cause: graph construction, the module quotient, SCC/realizability and
report serialization together are under 2% of samples on either engine, and
the devel run spends less wall after the last marker than the pin. The
bisect (below) confirms the two large causes are independent: B1 has the
full edge flood at the pin's value-flow cost, B3 has the full value-flow cost
at a third of devel's interpreter cost.

## Bisect

Three intermediate `-c opt` engines, built from the ducktape root on RBE
(`bbr build -c opt --config=nolint //devinfra/js/debundle:debundle`, all
action-cache warm so minutes rather than the budgeted 100 min: invocations
`acd6a865` B1, `6f5b1960` B2 after one `pnpm install` abort retry, `3f37a245`
B3) and run once each through the same harness as the control runs. Unlike
the pin and devel engines these are the root module's target-configuration
binaries, not the consumer's `ducktape+` exec-configuration build; same
source, same `-c opt`.

| Point | Commit       | Why                                                                                             |
| ----- | ------------ | ----------------------------------------------------------------------------------------------- |
| B1    | `e78dd6eeb6` | #12685, last commit before the shared-inference migration; deferred-callback inference included |
| B2    | `2dc4cc3768` | #12762, end of the migration, before the performance follow-ups #12792/#12799                   |
| B3    | `13de00c36e` | #12799, after the follow-ups, before the 10-29 native-input / Set / heap-frontier batch         |

Control runs (wall s; HWM at stage end in GiB):

| Engine             | Verdict  |    Wall |       Peak RSS |  Vendor purity stage | Main chunk analysis |     Edges | Eager / sequenced     |
| ------------------ | -------- | ------: | -------------: | -------------------: | ------------------: | --------: | --------------------- |
| pin `ecb4a734`     | accepted |    87.5 |           3.24 |                 31.0 |                29.3 | 2,163,669 | 1,109,226 / 1,033,882 |
| B1 `e78dd6eeb6`    | rejected |   106.6 |           3.92 |                 32.1 |                49.8 | 2,900,020 | 1,480,941 / 1,398,518 |
| B2 `2dc4cc3768`    | killed   | >11,572 | >9.1 (growing) | >11,560 (unfinished) |                   – |         – | –                     |
| B3 `13de00c36e`    | rejected |   262.6 |           4.77 |                184.5 |                56.9 | 2,900,020 | 1,480,941 / 1,398,518 |
| devel `4377a57bae` | rejected |   444.5 |           4.98 |                249.2 |               179.7 | 3,146,382 | 1,605,183 / 1,520,638 |

B2 was stopped by hand after 3.2 h, still inside the vendor purity stage it
had entered at 11 s, with RSS at 9.1 GiB and rising (the run's partial
`markers.tsv` and `rss_timeline.tsv` are kept); #12799's own message reports
a 2,237 s canonical replay on its parent, so this is the expected state of
the shared-inference solver before #12792/#12799, not a hang. What the three
points say:

- **Cause 1 (vendor value flow) is the shared-inference migration**
  `39daf9f536`…`2dc4cc3768` (#12705–#12762): B1 is at the pin's 32 s, B2 does
  not finish in 3 h, B3 recovers to 184 s after #12792/#12799, and the 10-29
  batch adds the last 65 s (184 → 249 s).
- **Cause 2's wall (main-chunk interpreter) is the 2026-10-29 batch**
  `b797753e91`…`884df2512d` (#12828–#12878): B3 spends 57 s there against
  devel's 180 s (pin 29 s).
- **The edge flood and the gate rejection are in place by B3**: 2.90M edges
  (+372k of the +496k eager), 990 `callback_pool_limit` and 379
  `construction` losses (devel 1,107 and 357), 1,195 incomplete units (devel
  1,306), the same five conflict groups (211, 7, 9, 2, 3 members). **And
  already by B1**: B1's graph is edge-for-edge the same size as B3's
  (1,480,941 / 1,398,518), with the same 990 / 379 losses, 1,195 incomplete
  units and the same five groups, so the barrier flood and the rejection
  come from `ecb4a734`…`e78dd6eeb6` (#12486–#12685: deferred-callback
  inference #12517/#12582/#12656, construction validation #12631/#12633,
  heap-content ordering #12598), before the solver migration. The 10-29
  batch then adds the last +124k eager edges, +117 `callback_pool_limit`
  losses and +111 incomplete units.
- **B1's wall is 107 s against the pin's 88 s**: +20 s in the main-chunk
  interpreter (49.8 vs 29.3 s) and +11 s before the purity stage, with no
  value-flow change. The engine that first rejects this corpus is only 1.2×
  slower than the one that accepts it; the 4–5× comes later and separately.

## Not measured

- Only the web corpus; the CLI corpus (43.5 s → 87 s, 4.40 → 9.62 GiB in
  step 0) was not run here.
- Single control run per engine on a shared host; differences under about
  10% are unresolved.
- Stage boundaries are the engine's five coarse markers; `compute_chunk_analysis`
  is not split into facts, interpreter and graph build by markers, only by
  profile frames.
- Peak RSS by stage is `VmHWM` at marker boundaries; allocation sites were not
  profiled (no heaptrack/massif run).
- The bisect engines ran once each, unprofiled; B2 was stopped before
  completing, so its edge counts and later stages are unknown.

Private detail (statement text, unit ordinals, module paths, conflict members)
is in the private repository's `tana/re/web/debug/2026-10-29-pin-vs-devel-perf.md`.
