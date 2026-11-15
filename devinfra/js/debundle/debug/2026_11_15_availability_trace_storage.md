# Lossless availability trace storage

The final evaluation-schedule guard records a sequence of cell states at each
qualified owner, operation path and cell. Repeated conservative callback
promotion can append the same state at the same point many times. A vector of
fresh `BTreeSet`s retains an allocation for every such observation, even when
all observations agree.

`AvailabilityTrace` stores consecutive equal state sets as runs. Append and
concatenation preserve the sequence, including its multiplicity. Concatenation
under an assumption changes every contributed observation to `Assumed`, as
before; it does not change `Unknown` into a known state. The schedule comparison
filters assumed observations before expanding the runs. The unknown-state
predicate can inspect each run once because repetition does not affect that
predicate. Saved aggregate availability and the artifact wire format do not
change.

This is a representation change, not a callback execution cache or an
availability proof. Every existing read still records its current state.

## Rejected callback graph experiment

A separate experiment cached pure retained-callback reachability with reverse
edge invalidation and strongly connected components. The mutation, cycle,
namespace, captured-cell and cross-chunk tests passed, but matched native
registration measurements rejected it: at 2,048 registrations the baseline
analysis took 19.07 seconds, the initial graph took 74.03 seconds, and incremental
invalidation with batched bitmap union still took 31.12 seconds. No graph cache
from that experiment is included here. Its patch and raw profiler artifacts are
retained in the task scratch directory.

The fixed baseline is the integrated `99a8ac6ffe` source with optimized CLI
artifact `012e7b2c30e77150ddfbb07f0206dcfde752038d5ff9d4311086a0eefe61baee`.
A 90-second canonical analysis sample reached approximately 4.3 GiB RSS without
completing. Its DWARF profiler capture recorded 27,267 samples and 327 lost
samples; Rust stacks did not unwind sufficiently for reliable inclusive costs.
The flat profile identifies substantial value comparison/allocation work, but
it cannot establish a whole-path speedup.

## Validation

Optimized invocation `f6f6291c-952f-4386-8747-f0e29e519d6f` passed the analysis,
bounded callback heap, cross-chunk operation parity, evaluation schedule, and
saved analysis parity targets. The commutation target passed 24/28; its four
failures match the existing post-precision baseline failures, including their
diagnostics. The trace unit test checks concatenation, run merging, preserved
observation multiplicity and the separate `Assumed`/`Unknown` states.

The candidate CLI artifact is
`809bf124c92457c4d028888be3b48e7827c6a86fd568f8b271bd2ea0284ec656`.
Registration analyses at 512, 1,024 and 2,048 entries produce byte-identical saved
analysis bodies compared with the fixed baseline (excluding the version
header). Initial wall times were 1.02, 3.81 and 22.79 seconds versus 0.95, 4.10
and 19.07 seconds. A matched repeat using the same 2,048-entry projection took
20.69 seconds for baseline and 20.17 seconds for the candidate. These runs do
not establish a registration speedup.

Canonical candidate analysis did not finish within its 120-second diagnostic
cap: 120.12 seconds wall, 364.74 seconds user CPU and 1,597,596 KiB peak RSS.
At approximately 90 seconds its process RSS was 1,469,920 KiB, versus
4,494,848 KiB for the baseline at 87 seconds. This supports a substantial trace
storage reduction, not a completed canonical analysis or a CPU speedup. The
remaining conservative callback-promotion cost is still unresolved.

## Batched source-root union

Finite callback promotion previously rebuilt a sorted `Values` union after
every binding evaluation. None of the intermediate unions is observed. The
replacement collects the same binding values in the same evaluation order and
constructs the sorted unique set once, before following callbacks or tainting
objects. It does not cache execution or skip any binding read.

Optimized invocation `7732b746-853e-4f45-9b1a-dc1d735f409f` passed analysis,
bounded callback heap (including the separate native-carrier cycle repair),
registration scale, cross-chunk operation parity and namespace live-carrier
tests. CLI artifact
`74016ba80c6ac243354a31fa7b1298f8c876d7b9d2622d1045cb3120826221a1`
produced the same saved analysis bodies at all three registration sizes. Wall
times were 0.91, 3.29 and 16.79 seconds at 512, 1,024 and 2,048 registrations;
the previous matched baseline/trace-only 2,048-entry runs took 20.69/20.17
seconds. This improves the registration workload but does not resolve canonical
cost: canonical analysis again reached its 120-second diagnostic cap
(120.13 seconds wall, 365.67 seconds user CPU, 1,596,576 KiB peak RSS).

Shared source preparation and execution include original bodies of
vendor-swapped modules. The vendor equivalence contract explicitly permits
applying facts from those bundled bodies to their replacements. This is not a
boundary defect. A conservative vendor execution limit would require separate
cost evidence and must preserve the existing full and partial swap contracts.

## Remaining callback-promotion work

A 60-second canonical profile of the optimized batched candidate, rebuilt with
`-Cforce-frame-pointers=yes`, collected 18,888 samples without lost samples.
`retain_foreign_callback_effects` accounted for 79.50% inclusive sample cost;
`promote_top_effects` accounted for 37.06%, `record_program_cell_read` for
24.97%, `record_runtime_binding_read` for 13.39%, and `evaluate_binding` for
8.44%. These inclusive costs overlap. Callback graph traversal was only 1.18%
and the existing callback memo lookup 1.12%. The profile does not justify
further graph-cache machinery or excluding vendor bodies. Repeated binding
and availability promotion is the next cost to address.

The former promotion preflight ran after every read had already recorded its
current availability and provenance. It searched those same qualified reads
again to decide whether to skip inserting rebind effects. Removing the
preflight preserves every actual read, its ordering and its multiplicity.
Rebind insertion already uses sets, preserves the first assumed path for held
effects and removes assumed tags when an effect is observed. In particular,
this change does not memoize execution, cell state or native activation.

For this comparison both binaries include the upstream merge, trace encoding,
native-carrier cycle repair and batched root union. Optimized baseline build
`bfbcd549-d106-4102-bb96-8c7e6815624f` produced CLI
`69fdf4e3b0f2bec36a2e5a2a5e2ac3e6bac890ade08834e9cedde87cc67d3143`;
candidate build `16b8bc2f-4026-468d-992d-bfcd73cfe1d0` produced
`e84c819b5a6793c06ea9e83de2fab8730a636f7c05ae2ad6c1cefb4e24bdd084`.
Registration analysis bodies remain byte-identical at 512, 1,024 and 2,048
entries. Baseline/candidate wall times were 0.92/0.90, 3.30/3.28 and
15.88/14.89 seconds, respectively. A reverse-order repeat of the same
2,048-entry projection took 14.69 seconds for candidate and 15.16 for
baseline. This supports a modest registration improvement (about 3–6% for
2,048 entries), not a resolution of canonical analysis cost. No further
unchanged canonical timeout run was performed.

Invocation `ba6efa6b-1b14-4268-81ca-fdb3dde7feab` passed the analysis unit tests,
callback heap's nine source/Node controls, four registration controls,
evaluation schedule controls and all 21 saved-analysis parity cases. The
cross-chunk suite passed 25 of 33 cases; eight stopped at their missing graph
report because the upstream report opt-in migration had not yet reached these
fixtures. This is separate from the performance change; the fixtures were
subsequently updated using the parent branch's graph-report migration.
The corrected cross-chunk suite passed all 33 cases in invocation
`b853eb7f-2523-4276-ae6e-61d71bebed57` with the same production change.
