# Frozen Excalidraw pipeline performance

The target is a 3× improvement in fresh `project → analyze → apply`, with
separate PRs for independent optimizations. The target is **not met**.
Reusing an analysis artifact measures the assignment-edit loop, not this target.

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
on the same host. Projection bytes and all 118 emitted JavaScript files match
in every run; the input manifest is checked afterward.

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
model, but its [independent audit](2026_11_10_statement_class_independent_audit.md)
found unsafe reorderings. Do not adopt it from historical zero-conflict counts.

The existing base classifier was checked with all optional upgrades,
derivation and premises disabled against the frozen editor's accepted order.
The source hash matches, and all 1,787 ordinals form an exact permutation.
The syntax census found no default export, direct eval, with, disposal,
top-level await or module-scope var nested in control flow.

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
