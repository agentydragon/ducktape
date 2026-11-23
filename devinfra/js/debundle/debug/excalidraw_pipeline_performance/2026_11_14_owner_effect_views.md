# Static owner effect views

## Workload and source

The fixed Excalidraw input/spec comes from
`df69d1c9d2ddc4d511bb44f9c796da6408fdbee3`: 79 chunks, 196 staged files,
and 118 emitted JavaScript files. The staged input manifest SHA256 is
`8154a3c4ce085b8cd0279f9473a56fa6ca84c622749bdf810781daa3220abfc3`.
The authored spec, source, package roots, reports, and thread configuration
were unchanged between measurements.

The measured parent is `fc6336654ba14c2e87b1c814d7d911ae3d5cb020`;
the candidate is `b0d446bbd257a8d3de7192b140c3c799dfd94fbc`.
Both use direct Bazel binary targets with `-c opt`, configuration
`2b32ee241a5b77b62378230fa58ace0fa849c9b61086c54b75e74227ff903c1a`.
The matching projector/analyzer/debundler artifacts are advertised by the
[parent build](https://app.buildbuddy.io/invocation/58c18390-7fed-4d58-9a3d-c3b47b9326b0)
and [candidate build](https://app.buildbuddy.io/invocation/5006dc87-8e72-4dee-877a-66d5fdb43b8b).
Downloaded executable sizes and SHA256 digests were checked against their
BuildBuddy artifact records.

## Profile and change

A separate frame-pointer build of the parent collected 5,773 samples with
zero lost samples and approximately 96.54 billion weighted cycles.
`top_restricted` accounted for 4.96% of analysis cycles, including 4.31%
under `promote_possible_getter`. These are overlapping inclusive costs.
This profiling build is separate from the normal opt timing comparison.

Static owner promotion repeatedly filters the same immutable read/rebind
sets against the same top-level binding set and allocates reference vectors.
The candidate builds sorted borrowed views once per execution input, retaining
both the prepared synchronous view and the existing incomplete-reachability
fallback. Promotion consumes slices. Initialization checks, live values,
provenance, and effect accumulation still happen at the current prefix.
Dynamic effect sets retain their existing filtering path.

## Fresh paired pipelines

Three alternating pairs ran fresh projection, analysis, and application with
all default reports. Each pipeline used its own matching tool trio and newly
written artifacts. Measurements used GNU time and `perf stat` for user cycles
and instructions. Analysis deltas, candidate relative to parent:

| Pair |   Wall |    CPU | Instructions | Cycles | Peak RSS |
| ---- | -----: | -----: | -----------: | -----: | -------: |
| 1    | -6.30% | +2.35% |       -8.75% | +1.01% |   +0.94% |
| 2    | -5.78% | -3.34% |       -8.72% | -3.05% |   +1.37% |
| 3    | -5.56% | -2.93% |       -8.77% | -2.98% |   -8.31% |

Stage medians:

| Stage       | Parent wall | Candidate wall | Parent CPU | Candidate CPU |
| ----------- | ----------: | -------------: | ---------: | ------------: |
| Projection  |       0.60s |          0.62s |      0.97s |         1.06s |
| Analysis    |      10.90s |         10.27s |     17.94s |        17.34s |
| Application |       2.98s |          4.46s |      4.02s |         4.02s |

Median analysis instructions fell from 158.609 billion to 144.717 billion.
Median analysis peak RSS was 847,240 KiB versus 852,500 KiB; individual
pairs varied. Full-pipeline elapsed ranges overlap: 13.97-20.07s for the
parent and 13.51-19.62s for the candidate. Their medians were 14.48s and
15.38s. Application timing varied despite nearly unchanged instruction
counts. This establishes reduced analysis work and lower paired analysis
elapsed times, not a reliable full-pipeline latency improvement or a 3x result.

Every projection matched byte-for-byte, all 118 emitted JavaScript hashes
matched, and every report JSON matched after replacing only its output-root
path. The full staged input manifest still matched after all six pipelines.

The measurement harness and raw counters are under
`/tmp/agents/2026_11_14/excalidraw-speed/preindexed-owner-pairs/`;
the task-local `compare-fresh.py` records every command and counter file.
The aggregate results above remain here when scratch is cleaned.

## Validation

[Exact candidate validation](https://app.buildbuddy.io/invocation/9b37b126-d242-47ef-8573-812a6ca3f98d)
passed 364 core analysis tests. All three Excalidraw test results were cached
after pipeline reconstruction. A separate
[focused CLI run](https://app.buildbuddy.io/invocation/10438cdf-8615-4133-a7b7-b312dda4c65f)
passed `callbacks/availability/future_availability.rs` and
`commuting_statements_gate_test`.
The PR checks validate its final head.
