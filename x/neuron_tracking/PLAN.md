# Plan

## First experiment

Run Codex on the example clip with a prompt, persistent annotation files, and
small deterministic helpers for extraction, rendering, validation, and export.
Keep one agent conversation for the clip, ideally entirely within one context
window. The agent should segment every identifiable pixel of each neuron's body
and branches, see exactly what those masks look like on the source images,
correct them, and carry identities across frames.

The first question is whether this workflow produces useful annotations on the
actual sample. A successful, repeated procedure can become a skill with the
helpers bundled alongside it. The current draft does not establish accuracy or
constitute a paved workflow.

## Evidence from the sample

`IMG_6128.AVI` decodes to 73 frames at `1280 × 944` over `36.4 s`.
Inspection of every frame in contact sheets, plus full-resolution views, shows
substantial changes in specimen position and orientation, variable focus and
brightness, and crowded dark features. These are moving views of the specimen;
the video alone does not establish acquisition timing or physical depth.

Use the small dark features in the JPEG's red outlined region as the intended
target. Treat its outline as guidance for finding the region, not as a mask that
already aligns with every video frame. Detailed evidence and reproduction
commands live in `debug/sample_inspection.md`.

## Annotation and correction loop

1. Inspect a timeline overview and full-resolution frames. Choose a clear anchor
   frame and a small group of separable target features for the initial trial.
2. Save separate full-resolution body and branch masks under each persistent
   neuron ID, following `annotation_format.md`. Record uncertain pixels
   separately. Both masks belong to the same neuron across frames.
3. Render those exact saved annotations over the original pixels. Show the agent
   both raw and annotated views, with matching zooms, readable IDs, and colors.
   Show the combined mask and each part separately. Use distinct fill/outline
   styling within the neuron's color so body and branches are distinguishable
   and underlying pixels remain visible.
4. Require the agent to open the rendered images, inspect placement and identity,
   and correct missing pixels, leaked background, wrong boundaries, body/branch
   classification, and identity errors at crossings. Check fine branches and
   tips at pixel-preserving zoom. Render and inspect again after changes. An
   output path or a successful renderer exit is not evidence that the agent
   visually checked the result.
5. Extend the same identities through neighboring frames in both directions.
   Compare raw and annotated crops across frames, including before and after
   pose changes. Add newly visible targets; record ambiguity and disappearance.
6. Inspect the final annotations for every output frame and review temporal
   comparisons. Export the reviewed revision, then inspect decoded frames from
   the encoded video as well. Revisions that change annotations invalidate the
   affected visual checks.

The agent may request crops, zooms, or image alignment to help comparison.
Classical tracking or segmentation can later propose masks if annotation is the
bottleneck; its proposals go through the same visible correction loop.

## One conversation and one context window

Keep the timeline, identity decisions, corrections, and visual checks in one
Codex session. Do not dispatch independent agents or start a fresh session per
frame. Save an explicit ID table and annotation revisions on disk throughout the
run; these are the export source of truth even while the conversation is intact.

Use contact sheets for orientation and selected full-resolution crops for
annotation. Budget for repeat views, output tokens, and growing conversation
history, not just a single image per source frame. Measure actual context use in
the trial before attempting all neurons in all 73 frames. A prompt alone cannot
guarantee that the harness will avoid automatic compaction: record whether it
occurred, and do not report a compacted run as a single-context success.

If the clip cannot fit, save partial results and report the limitation. A
different continuation strategy is a separate experiment.

## Small helper surface

Build only the helpers the trial actually needs:

- Extract lossless frames and source presentation timestamps with `ffmpeg` and
  `ffprobe`; generate labeled contact sheets.
- Validate and losslessly encode pixel masks; rasterize edits and render full
  frames, crops, per-part views, and temporal comparisons from a saved revision.
- Export that same revision to JSON, CSV, and an annotated MP4.

Give every displayed crop its source frame, dimensions, crop origin, scale, and
revision. Store masks on the original-frame pixel grid; helpers map them into
display coordinates. Test crop/zoom mappings and mask encoding with known pixel
regions before relying on annotations. Allow tracing and brush edits without
making the model emit individual pixel coordinates as text.

Rendering uses the source pixels and saved masks. Image generation is not
part of the overlay pipeline.

## Proposed outputs

| Artifact           | Contents                                                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `annotations.json` | Versioned source manifest, ID/color table, per-frame body/branch/uncertainty pixel masks, and review state.              |
| `tracks.csv`       | One row per neuron per frame: ID, index, source timestamp, body centroid, part areas, visibility, and review status.     |
| `annotated.mp4`    | Original frames and timing with persistent neuron colors and IDs, distinct body/branch styling, and visible uncertainty. |
| `review.json`      | Checked revision and frame coverage, opened overlay paths, remaining ambiguities, and any incomplete work.               |

Use zero-based frame indices and source image coordinates: pixel `(0, 0)` is the
center of the top-left pixel, `x` increases rightward, and `y` downward. Retain the
source timestamps rather than deriving them from an assumed constant rate.
Video timestamps are not automatically biological acquisition times.

Assign arbitrary IDs within a clip, not anatomical names or cross-clip
identities. Keep one ID/color assignment across every frame and export. Record
visibility as `visible`, `uncertain`, `not_visible`, or `out_of_frame`; missing
observations have empty masks and null derived centroids. Record ambiguous
boundaries and associations explicitly rather than forcing an identity through
a pose change. Distinguish proposed, agent-reviewed, and human-reviewed
annotations. Model self-review does not imply human verification.

## Paving and acceptance

- [x] Inspect the actual sample and record reproducible observations.
- [ ] Agree the bounded trial protocol: selected frames and targets, Codex
      provider/model, context and token limits, and acceptance criteria.
- [ ] Implement and exercise the minimal media and annotation helpers via Bazel.
- [ ] Run the draft prompt on a short sequence including a pose change. Include
      deliberately mislabeled pixels to check that visual feedback supports
      detecting and correcting an error; report the correction honestly.
- [ ] Review representative masks against the raw frames. Record missed branch
      pixels, background leakage, body/branch classification errors, identity
      switches, unresolved cases, token use, and context-window behavior. Measure
      per-part pixel overlap and thin-branch coverage if reference masks exist.
- [ ] Amend the prompt and helpers where the run required improvisation; repeat
      from a fresh output directory and Codex session. Each individual run still
      processes its sequence in one conversation.
- [ ] Extend to all 73 frames and all distinguishable targets in the region.
      Visually inspect every final overlay and check difficult transitions.
- [ ] Package the demonstrated procedure as a skill, with the tested helpers
      and known limits. Re-run from its instructions before calling it paved.

Before hand-off, machine checks must establish exact mask-encoding round trips,
valid integer pixel bounds, pairwise disjoint body/branch/uncertainty masks within
each neuron, unique `(neuron_id, frame_index)` observations, existing ID references,
a stable and distinct color map, and review coverage for the exported revision.
Check the encoded video's frame count, dimensions, timing, and overlays against that
revision. Synthetic fixtures can exercise these contracts without committing
the user's media. Biological correctness still needs inspection against the
source images; no recall or identity-accuracy target has been demonstrated yet.

## Codex entry point and provider

The installed CLI is `codex-cli 0.162.1`; its help exposes `exec`, `--image`,
`--sandbox`, `--json`, and `--output-last-message`. After helpers exist, a trial
can start from the repository root with a prompt along these lines:

```bash
mkdir -p x/neuron_tracking/runs/trial
codex exec --sandbox workspace-write --json \
  --output-last-message x/neuron_tracking/runs/trial/handoff.md \
  'Read x/neuron_tracking/annotate.md. Annotate the agreed trial frames from x/neuron_tracking/data/IMG_6128.AVI and save outputs under x/neuron_tracking/runs/trial.' \
  > x/neuron_tracking/runs/trial/codex-events.jsonl
```

This invocation has not been run. Use the current Codex authentication/provider
first. If CLIProxyAPI is used, verify its available model, image/tool support,
and context behavior on the same bounded trial. Keep endpoint and credentials
in local configuration. A separate API orchestration service is unnecessary for
this experiment.

Official OpenAI documentation describes [non-interactive Codex runs](https://learn.chatgpt.com/docs/non-interactive-mode)
and [skills](https://learn.chatgpt.com/docs/build-skills). Its
[vision guidance](https://developers.openai.com/api/docs/guides/images-vision)
also documents localization and resizing limitations; the crop-to-source
mapping and render-and-check loop need direct validation.
