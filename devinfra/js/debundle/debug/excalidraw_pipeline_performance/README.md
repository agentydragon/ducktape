# Frozen Excalidraw pipeline performance

The target is a 3× improvement in fresh `project → analyze → apply`, with
separate PRs for independent optimizations. The target is **not met**.
Reusing an analysis artifact measures the assignment-edit loop, not this target.

Paired measurements on this frozen workload also cover
[indexed prefix reads](2026_11_14_indexed_prefix_reads.md) and
[static owner effect views](2026_11_14_owner_effect_views.md).

## Workload and measurement

The input/spec closure is frozen from
`df69d1c9d2ddc4d511bb44f9c796da6408fdbee3`: 79 JavaScript chunks,
196 files including package roots, 5,409,212 bytes. The SHA-256 of its sorted
file-hash manifest is
`8154a3c4ce085b8cd0279f9473a56fa6ca84c622749bdf810781daa3220abfc3`.
The canonical projection hash is
`dfb8b726fdfddedc87372f755a77f0bdb717698452d90f896c7b11b90e4d1a7f`.
Subsequent spec expansions are outside this comparison.

Each native run uses optimized executables acquired from the corresponding
RBE Rust compilation, physical absolute source/package roots, and a fresh
output directory. The projector writes a new projection, the analyzer reads
that projection and writes a new artifact, and the normal `debundle run`
reads that artifact and emits the complete tree and default reports. No
budgets, report settings, analysis inputs or thread settings are changed.
`perf stat` records user instructions, user cycles and task clock;
`/usr/bin/time` records elapsed time. Three baseline/candidate pairs alternate
on the same host. For the November 14 pairs, projection bytes and all 118 emitted JavaScript
files match in every run; the input manifest is checked afterward.

The baseline executables came from BuildBuddy invocation
`8c749d09-5aff-50b2-ad76-139b8c880a83`. That invocation also ran all three
actions freshly on RBE: projection 4.978 s, analysis 23.713 s, apply 11.576 s.
Their sum is 40.267 s; the 53.128 s span includes scheduling gaps.
Those timings must not be compared directly with native times below.

Saved artifacts bind the engine identity as well as inputs. Profiling builds,
target builds and exec builds can have distinct identities. Acquire a matching
projector/analyzer/debundler trio; never edit artifact headers to mix binaries.
Frame-pointer profiles are separate from the normal native benchmark.

## Merged optimizations

- [Buffer analysis decoding](https://github.com/agentydragon/ducktape/pull/16481):
  wrap the decompressor in a buffered reader before MessagePack deserialization;
  still drain it to detect a corrupt trailing compressed frame.
- [Borrow selector declarators](https://github.com/agentydragon/ducktape/pull/16482):
  extract selector facts from borrowed declarators rather than cloning the
  declaration and initializer for each index row.

Combined optimized tools were measured before merging and then rebased onto
the merged changes. Median instruction counts, billions:

| Stage         | Baseline | Both changes | Reduction |
| ------------- | -------: | -----------: | --------: |
| Projection    |   11.035 |        3.709 |     66.4% |
| Analysis      |  202.656 |      202.667 | unchanged |
| Apply         |   40.603 |       27.655 |     31.9% |
| Full pipeline |  254.281 |      234.032 |      8.0% |

Full-pipeline elapsed seconds for the three alternating pairs:

| Pair | Baseline | Both changes |
| ---- | -------: | -----------: |
| 1    |    18.91 |        19.27 |
| 2    |    18.70 |        18.60 |
| 3    |    23.91 |        16.83 |

The baseline's third apply took 8.39 s despite normal instruction counts.
Wall ranges overlap substantially. These measurements establish reduced work,
not a clear full-pipeline wall-time gain. Combined staged parity/identity,
Excalidraw output, tree and browser checks passed (five targets, invocation
`028449ed-f9b8-4807-89f3-f2f8bbebd5b4`).

## Remaining cost and rejected experiments

A frame-pointer analysis profile has 6,229 samples and no lost samples.
Source-order interpreter execution accounts for about 81% of sampled cycles.
Callback-pool processing, getters, recursive invocation and fallback overlap;
their inclusive percentages cannot be added. Invocation plus getter and lazy
promotion families cover 68.7% of sampled cycles. A cache cannot erase all of
that work, so invocation caching alone has insufficient measured scope.

Three unpublished candidates were measured: callback-result collection
changes, deduplicating static callable reads within an unsettle pass, and a
guarded callback-graph walk memo. Instruction changes were respectively about
−1.2%, +0.5% and −1.3%; wall ranges overlapped. None was published as a speedup.
The walk memo also added heap/read-set invalidation complexity.

A one-pass Rayon sweep measured 21.81 s at one thread, 14.42 s at two,
13.93 s at four, 13.85 s at eight and 14.46 s at the unset default.
All artifacts were byte-identical. Analysis already parallelizes chunks; the
default-to-eight difference is too small and unreplicated to justify a change.

The 18.91 s baseline median implies a target near 6.3 s. With projection
around 0.5 s and apply around 3.4 s after these changes, analysis would need
to fall from roughly 14 s to roughly 2.4 s unless apply also improves.

## Statement-summary stopping gate

The report-only statement-class prototype has a substantially cheaper cost
model, but its [independent audit](../summary_effects/2026_11_10_statement_class_independent_audit.md)
found unsafe reorderings. Do not adopt it from historical zero-conflict counts.

The existing base classifier was checked with all optional upgrades,
derivation and premises disabled against the frozen editor's accepted order.
The source hash matches, and all 1,787 ordinals form an exact permutation.
The syntax census found no default export, direct `eval`, `with`, disposal,
top-level `await` or module-scope `var` nested in control flow.

Of 253,092 inversions, the prototype leaves 34 unproven with its optimistic
no-cycle export roots. Those involve two relocated class definitions.
Production selects `ExportReentryPolicy::Conservative`; using the prototype's
broader `--exports-reachable` roots leaves 53,448 pairs unproven. That flag is
an approximation of production's root policy, not a complete adapter. Both
runs refuse the frozen assignment, so this route failed the precision
stopping gate. The broader run is recorded in BuildBuddy invocation
`65e6838b-95b8-4e0f-b6c9-c1ae9235562c`. These are report-only results, not a
verified speedup or evidence that those moves are unsafe.

A future checked producer must preserve throwing
initializers and TDZ observations, reject unsupported scope/default-export
shapes, and consume production export-reentry policy. Eligibility reads only
source, semantic projection and engine, never requested ownership. The next
useful proof investigation would reduce one reach conflict and establish a
sound source-only rule with an unsafe sibling, then check it against the
actual conservative roots. The present evidence does not justify production
integration or enabling the prototype's optional rules.

Any production route needs explicit typed saved facts, truthful diagnostics,
neutral real-CLI `bundle + spec → outcome` cases covering the audit failures,
staged parity, emitted behavior and a fresh full-pipeline comparison. A
summary is not an interpreter state transfer: skipping selected statements
and then resuming the mutable-world interpreter is not justified.

## November 16 consolidation checkpoint

This comparison brackets the five consolidation slices and the passive-provider
follow-up: baseline `744d4896e8db2cb76787e68657d03be0bb48353e`, immediately before
#16971, and current `811320408e8ab3647e060d974820879c373e7401`, after #17044.
It compares complete engine revisions, including intervening changes; it does
not attribute a difference to one slice. Both matching trios use ordinary
`-c opt` target builds, with no profiling compiler flags.

The same frozen 196-file closure above is checked before and after each run.
The historical config predates mandatory `import_order`: current projection
and analysis succeed on it, but apply refuses an all-entry chunk without that
field. Conversely, the November 14 engine rejects the new field during
projection. Neither incomplete attempt is counted as a completed benchmark.

For the matched comparison, both November 16 engines receive the original
config plus one identical explicit order witness: each chunk's named modules
follow the historical successful entry's static import order; all-entry chunks
use `[]`. The witness covers exactly the 30 editor and 10 main named modules,
each once. Sources, assignments, premises, reports, limits, package roots and
thread settings are unchanged. The augmented config SHA-256 is
`e487c3219d41191458480971557ca4443d2fde09f8b72846fadf1358d5826249`.
The shared projection SHA-256 is
`9d424dac670d1548676d400169af9986e2293589d9fd188e78cfea3278e449a1`.

Native measurements on wyrm2, x86-64, Linux 6.18.55, 32 logical CPUs, with
`RAYON_NUM_THREADS` unset, use the same `perf stat` and `/usr/bin/time` clocks
as above. The three pairs run sequentially in current/baseline,
baseline/current, current/baseline order. Every phase exits successfully.

| Pair | Before consolidation | Current |
| ---- | -------------------: | ------: |
| 1    |              16.43 s | 14.14 s |
| 2    |              12.91 s | 14.12 s |
| 3    |              13.37 s | 14.46 s |

| Stage         | Before, median instructions (B) | Current (B) | Change |
| ------------- | ------------------------------: | ----------: | -----: |
| Projection    |                           3.720 |       3.701 |  −0.5% |
| Analysis      |                         143.962 |     154.154 |  +7.1% |
| Apply         |                          25.175 |      30.500 | +21.2% |
| Full pipeline |                         172.858 |     188.354 |  +9.0% |

Full-pipeline median wall time is 13.37 versus 14.14 seconds; the ranges overlap.
Analysis medians are 9.84 versus 10.55 seconds, with median peak RSS 829.2 versus
891.5 MiB. The stable instruction increase establishes more work on this
workload. These three pairs do not establish a general wall-time regression,
a per-PR cause, or a speedup. The original 3× goal remains unmet.

Each engine emits the same 118 paths, byte-identically across its own repeats.
Across engines, 14 editor modules reorder identical static import declarations;
all other lines match. Import order can affect ESM evaluation, so this is
**not** cross-version byte or runtime parity. Both versions pass their ordinary
pipeline gate; these measurements add no browser/runtime equivalence proof.

Build provenance, with downloaded executable sizes and SHA-256 verified against
BES artifacts:

- Baseline: [explicit-commit build](https://app.buildbuddy.io/invocation/b6f42b70-1749-486f-b6d2-1201b1537659).
  Its three binary hashes match the original measured build
  `5f79656b-2aea-4512-a8e6-b4aafe02400c`.
- Current: [explicit-commit build](https://app.buildbuddy.io/invocation/b952d68d-efaa-45e9-871e-31b9932a9cbf).
  Its three binary hashes match the measured build
  `cc389d94-3274-43cb-ac93-54a2bcca9b79`.
- Caller-attribution build: [optimized with frame pointers](https://app.buildbuddy.io/invocation/1b3b23b4-1f4a-4427-aa84-c20d36ea4582).
  This separate matching trio is used only for sampling, never for the timing
  comparison. Normal-build DWARF unwinding lost most caller stacks, so its flat
  self-cost report alone cannot support inclusive callback percentages.

### Remaining work and the next bounded case

A completed 99 Hz user-cycle frame-pointer profile has 2,825 samples and zero
lost samples. Inclusive attribution is 71.2% under synchronous-call annotation,
43.6% under `invoke_possible_callbacks_transfer`, 38.6% under
`invoke_callback_pool_once`, 17.5% under bounded invocation fallback and 9.9%
under `walk_callback_values`. The stack-membership union of invocation,
possible-getter and lazy-owner promotion is 57.6%. These categories overlap;
none of their percentages should be added or interpreted as removable cost.
The profile attributes CPU samples, not wall time, invocation counts, individual
JavaScript call sites or the instruction increase between versions.

The source explains the repeated work that remains. Opaque invocation builds
an available callback pool, executes members against an evolving world and
compares broad world observations; a changing pool/world repeats that pass.
Fallback retains effects incrementally, but still collects argument/capture/
export/escape values, rediscovers callback reach, unsettles reachable objects,
and forms owner roots at each request. Its existing incremental owner traversal
is useful reuse, not a reason to claim that all fallback work is incremental.
`read_only_callbacks.rs` bypasses replay only when the **complete pool** consists
of admitted local lexical observers with currently primitive or unavailable
returns. A closure, foreign value, call, heap operation or another unsupported
member can refuse that shortcut. This is an eligibility boundary established
by code, not a measured count of missed pools.

A small related reproducer isolates unnecessary receiver reach:

```js
import { host } from "./peer.js";
const entries = [() => later];
entries.forEach(() => host(1));
const first = entries[0];
const later = 42;
console.log(first());
```

The peer is `export function host(value) { console.log(value); }`. Request a
`later` module containing only `later`, and inline the remainder. Original and
inline-emitted execution print `1` then `42`. The split is valid for this
program: the host receives neither the array nor its stored callback.
Current analysis nevertheless promotes a read of `later` during the iteration
and the following indexed read, which is reported as an unsupported member
read. Those statements and `later` form an atomic group, refusing the split.
Removing the opaque call admits the same extraction. Passing the third
iteration argument (the array) to a host that invokes its first element really
prints `ReferenceError` before `42`; that split must stay rejected.

These three real CLI controls live in
[`callbacks/iteration_receiver.rs`](../../e2e/callbacks/iteration_receiver.rs).
The safe case deliberately records the current conservative refusal, alongside
original/emitted execution. A future proof must change it to accepted emitted
execution; this checkpoint does not fix it or weaken the gate. This case
exercises the profiled getter/callback machinery, but sampling alone does not
establish its frequency or potential Excalidraw savings.

The next bounded implementation is a capability proof for a confined dense
iteration receiver across primitive-only opaque operands. It must preserve
receiver slot knowledge without certifying the opaque call's completion or
discarding its other effects. The precise admission, dependency and work-bound
requirements belong in the existing
[consolidation plan](../../plans/analysis_consolidation.md#next-bounded-callback-slice).
No broad callback-engine rewrite or LRU/world-result cache is justified by this
checkpoint. Measure the same complete pipeline again after that independently
reviewable slice; retain the 3× target and the conservative unsupported cases.
