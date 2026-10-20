# First five annotation trials

Five independent `gpt-6-luna` / `xhigh` conversations processed the sample.
All reached hand-off, including interrupted jobs resumed in their original
conversations. Compaction is allowed. Native traces and all generated files
are stored under `~/drive/neuron-tracking/runs/luna_xhigh_0{1,2,3,4,5}/`;
no media or annotation blobs are committed. Full native Codex transcripts are
in `~/drive/neuron-tracking/transcripts/`.

## Results and direct inspection

Every run supplied JSON, schema, CSV, AVI, review record, and hand-off text.
All included records for 73 source frames. This does not establish complete
target coverage or accurate identities.

| Trial | Returned annotation scope                                                | Inspection of decoded AVI frame 68                                                        |
| ----- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| 1     | One provisional ID; only uncertainty pixels, empty body and branch masks | Many candidate patches share one ID; this is not individual-neuron segmentation.          |
| 2     | Seven candidate IDs                                                      | Several labels and masks are on background outside the specimen; tracking drift remains.  |
| 3     | Two IDs, definite masks concentrated in frames 55–72 and 64–72           | Two masks align with visible dark branching features, but many other targets are omitted. |
| 4     | 92 candidate IDs plus an unresolved group `U001`                         | Several candidates are on background; many visible branching features are omitted.        |
| 5     | One ID, definite masks in frames 64–72                                   | One mask aligns with a visible branching feature; other targets are omitted.              |

These are qualitative observations from representative outputs, not measured
pixel accuracy or a human-authored reference annotation set. The agents report
broader visual reviews in their native traces and `review.json`; their self-review
did not prevent the errors above. No run demonstrates a complete neuron census.

## Machine checks

Independent checks passed for all five final JSON files: supplied JSON Schema,
source SHA-256, source frame timestamps, unique ID/color assignments, one
observation per ID per source frame, pixel bounds, sorted and coalesced runs,
disjoint body/branch/uncertainty masks within an observation, and empty masks for
absent observations. Structural validity does not establish biological accuracy.

All AVIs decode at `1280 × 944`. Trials 1, 2, 3, and 5 decode to 73 frames and
use constant cadence, with roughly 0.1 s maximum displacement from the source
presentation timestamps. Original timestamps remain in the JSON. Trial 4
preserves the first 73 source timestamps but adds a terminal duplicate, producing
74 decoded frames and a 36.42 s duration. Its AVI header reports 36,420 frames;
counting decoded frames reveals the actual 74. The added frame violates the
requested frame count even though its hand-off documents the compromise.

The shared renderer and validator in `TODO.md` should remove these differences
between exports. Their video checks need to decode the output and compare it
against the saved annotation revision and source timing.

Unexpected process exits and the observed service-managed continuation are
documented in [the abort investigation](parallel_run_aborts.md).
