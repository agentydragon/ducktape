# First complete test-list architecture screen

The source-pinned A/B/B/A screen at `376f8ae15b5878320892e2d1c42add6a8361b2c3`
passed all four arms. It replays the historical `2196229` test-label population,
not current affected-target selection or complete required GitHub feedback. Both
arms share all source, including eight Bridge shards.

| Arm                                                                             | Composition             | Native elapsed | Physical test launches | JUnit cases | Known test CPU |
| ------------------------------------------------------------------------------- | ----------------------- | -------------: | ---------------------: | ----------: | -------------: |
| [A1](https://app.buildbuddy.io/invocation/299737f4-5194-4436-a538-ce54f3466fff) | Full Pod/VM, 128 labels |       467.576s |                    138 |       1,026 |    10,880.215s |
| [B1](https://app.buildbuddy.io/invocation/b4caf1e2-5d2f-4aa6-908b-534c6fce737d) | Candidate, 133 labels   |       411.005s |                    143 |       1,033 |    11,645.417s |
| [B2](https://app.buildbuddy.io/invocation/26ff9be2-9e80-4c80-bc2e-f3f50557d346) | Candidate, 133 labels   |       319.945s |                    143 |       1,033 |    10,708.067s |
| [A2](https://app.buildbuddy.io/invocation/56ea9425-e24f-4b08-a8e0-3ca13dadda88) | Full Pod/VM, 128 labels |       417.391s |                    138 |       1,026 |    10,329.645s |

Mean native elapsed is **442.484s versus 365.475s**, an observed reduction of
77.009s (17.4%). Known CPU rises 5.4%. Each arm has one unchanged Firecracker
action with unknown CPU and queue clocks. These are reported-resource sums,
not full CPU or billing figures.

All 562 physical test launches are unique NEW executions with nonempty JUnit,
first run/attempt only, and zero failures, errors or skips. Unchanged targets
retain their collected case identities. The Pod slices retain the exact disjoint
5+16 union of all 21 cases. Candidate additions account for only 14 fresh target
launches across both runs; 4,118 case exposures are not independent trials or
the adoption requirement's 100 fresh contract executions.

## The VM speed hypothesis remains unproved

The full VM case envelope averages **171.775s**, versus **173.669s** for the
smaller witness. Both full VM baselines rebuild infrastructure after a fingerprint
mismatch; both witnesses reuse a warm baseline with the same compatibility
fingerprint. Setup averages 110.552s versus 36.054s. The apparent VM process
reduction is dominated by this unmatched cache exposure, not less case work.

The first candidate's Pod slices also differ in setup: 69.139s versus 117.729s,
including 18.544s versus 65.178s of serial image imports. Their case envelopes are
123.875s and 171.990s. The repeat reuses warm infrastructure and skips all measured
imports. Rebalancing alone cannot explain or remove that variation.

In B2 the deployed witness and MCP Services finish about four seconds apart;
Pod is the next tail. A VM-only or service-only improvement cannot be translated
directly into complete feedback savings.

## Next experiment

Prototype a manual single-VM **platform** witness without duplicate model turns:
real issuer/Pod binding, authenticated Runtime readiness, unique workspace work,
real CLI routes and post-operation identity. Both packaged guest contracts retain
the harness setup/fence, two turns and exact History. Review private-kernel,
launcher, mount and supervision ownership and seed defects before promoting it.
The current full VM gate, test frequency and separate OOM acceptance stay intact.

The existing importer draft #15126 remains unproven. A repaired matched benchmark
is conditional on cold-import frequency, not a new optimization claim. Natural
backend-head and CaseBatch/PVC follow-ups remain in #16028/#16034 for November 20.

[Reviewed measurements, target sets and phase provenance](test_architecture_graph_screen_376.json)
retain the limits. This screen establishes neither p90, fleet reliability,
complete-CI savings, coverage equivalence nor diminishing returns.
