# Plan

## First experiment

Run Codex on the example clip with a prompt and the expected annotation schema.
Use `gpt-6-luna` with `model_reasoning_effort="xhigh"`. Codex may obtain programs
through Nix and write its own processing scripts during the run. Use the shared
renderer for saved-mask previews and AVI export.
Keep one agent conversation for the clip; context compaction is allowed.
The agent should segment every identifiable pixel of each neuron's body
and branches, see exactly what those masks look like on the source images,
correct them, and carry identities across frames.

The first question is whether this workflow produces useful annotations on all
73 sample frames. A successful, repeated procedure can become a skill; bundle
helpers only where actual runs demonstrate their value. The current draft does
not establish accuracy or constitute a paved workflow.

## Evidence from the sample

`IMG_6128.AVI` decodes to 73 frames at `1280 × 944` over `36.4 s`.
Inspection of every frame in contact sheets, plus full-resolution views, shows
substantial changes in specimen position and orientation, variable focus and
brightness, and crowded dark features. These are moving views of the specimen;
the video alone does not establish acquisition timing or physical depth.

For this sample, use the neurons along the embryo's side/belly in the JPEG's red
outlined region as the intended target; see `sample_task.md`. Keep this constraint
out of the general workflow and renderer. Treat the outline as guidance, not as a mask that
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

The agent chooses its tools and remains responsible for complete annotation
and identity review in every frame. Sampling a few successful examples does not
validate the rest of the sequence. The annotation instructions are in
`annotate.md`; evaluate their effect against the source and the saved outputs.

## Conversation continuity

Keep the timeline, identity decisions, corrections, and visual checks in one
Codex session. Do not dispatch independent agents or start a fresh session per
frame. Save an explicit ID table and annotation revisions on disk throughout the
run; these are the export source of truth even while the conversation is intact.

Use contact sheets for orientation and selected full-resolution crops for
annotation. Budget for repeat views, output tokens, and growing conversation
history, not just a single image per source frame. Compaction is allowed and is
not a failure condition. Persist unresolved identity decisions and review
progress alongside the masks so the same conversation can continue after
compaction or a resumed CLI process. Save partial results and identify missing
coverage if a resource limit prevents completion.

## Processing during the run

Let the annotation agent choose and execute the processing it needs, using the
repo's Nix environment, `nix shell`, or `nix-shell`. It may create scratch scripts
under its assigned Drive output directory for these operations:

- Extract lossless frames and source presentation timestamps with `ffmpeg` and
  `ffprobe`; generate labeled contact sheets.
- Validate and losslessly encode pixel masks; rasterize edits and render full
  frames, crops, per-part views, and temporal comparisons from a saved revision.
- Export that same revision to JSON, CSV, and an annotated AVI, including a copy
  of `annotations.schema.json` with the machine-readable data.

Give every displayed crop its source frame, dimensions, crop origin, scale, and
revision. Store masks on the original-frame pixel grid; helpers map them into
display coordinates. Test crop/zoom mappings and mask encoding with known pixel
regions before relying on annotations. Allow tracing and brush edits without
making the model emit individual pixel coordinates as text.

Rendering uses the source pixels and saved masks. Image generation is not
part of the overlay pipeline.

## Proposed outputs

| Artifact                  | Contents                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `annotations.json`        | Versioned source manifest, ID/color table, per-frame body/branch/uncertainty pixel masks, and review state.                     |
| `annotations.schema.json` | JSON Schema defining the annotation structure; copy the supplied schema alongside the data.                                     |
| `tracks.csv`              | One row per neuron per frame: ID, index, source timestamp, body centroid, part areas, visibility, and review status.            |
| `annotated.avi`           | Source frame order and dimensions with persistent neuron colors and IDs, distinct body/branch styling, and visible uncertainty. |
| `review.json`             | Checked revision and frame coverage, opened overlay paths, remaining ambiguities, and any incomplete work.                      |

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
- [x] Select `gpt-6-luna` with `xhigh` reasoning; allow Nix and processing scripts
      created during execution.
- [x] Supply the prompt, pixel-mask semantics, and machine-readable JSON Schema.
- [x] Launch five independent runs on the supplied clip, each processing its
      sequence in one conversation with a visual correction loop.
- [ ] Review representative masks against the raw frames. Record missed branch
      pixels, background leakage, body/branch classification errors, identity
      switches, unresolved cases, token use, and context-window behavior. Measure
      per-part pixel overlap and thin-branch coverage if reference masks exist.
- [ ] Amend the prompt and helpers where the run required improvisation; repeat
      from a fresh output directory and Codex session. Each individual run still
      processes its sequence in one conversation.
- [ ] Confirm coverage of all 73 frames and all distinguishable targets in the
      region. Visually inspect every final overlay and difficult transitions.
- [ ] Package the demonstrated procedure as a skill, with the tested helpers
      and known limits. Re-run from its instructions before calling it paved.

Before hand-off, machine checks must establish exact mask-encoding round trips,
valid integer pixel bounds, pairwise disjoint body/branch/uncertainty masks within
each neuron, unique `(neuron_id, frame_index)` observations, existing ID references,
a stable and distinct color map, and review coverage for the exported revision.
Check the encoded AVI's frame count, dimensions, timing, and overlays against that
revision; record any changes to source presentation timing. Synthetic fixtures can exercise these contracts without committing
the user's media. Biological correctness still needs inspection against the
source images; no recall or identity-accuracy target has been demonstrated yet.

## Codex entry point and provider

Media and run data live in `~/drive/neuron-tracking/{data,runs}/`, outside
the checkout. The launch command is in `README.md`. The installed CLI is `codex-cli 0.162.1`;
its model cache lists `gpt-6-luna`, `xhigh` reasoning, and image inputs. The CLI
reports an existing ChatGPT login. The five trials use that login and provider;
their partial annotations do not yet establish complete or accurate tracking.

Use the current Codex authentication/provider first. CLIProxyAPI is an available
alternative if desired; keep endpoints and credentials in local configuration.

Official OpenAI documentation describes [non-interactive Codex runs](https://learn.chatgpt.com/docs/non-interactive-mode)
and [skills](https://learn.chatgpt.com/docs/build-skills), and lists
[`gpt-6-luna` with `xhigh` support](https://developers.openai.com/api/docs/models/gpt-6-luna). Its
[vision guidance](https://developers.openai.com/api/docs/guides/images-vision)
also documents localization and resizing limitations; the crop-to-source
mapping and render-and-check loop need direct validation.
