# Neuron annotation prompt

Produce annotations and an annotated video for the supplied microscopy clip.
This is the first experiment; perform the work and report what you achieved.

Use the input clip, reference JPEG, and output directory supplied in the launch
prompt. Process the whole clip and every distinguishable neuron in the target
region, including neurons that become visible later. The reference's red outline
indicates the target region; it is not an already aligned segmentation mask.
Start with a clear frame to establish the annotation/review loop, then continue
through the clip without waiting for intermediate approval.

Create any extraction, processing, mask-editing, rendering, validation, or video
encoding scripts you need as you work. Use the repo's Nix environment or obtain
additional programs through `nix shell` or `nix-shell`. Keep scratch scripts and
generated artifacts in the output directory; no prebuilt helpers are required.
Choose useful processing methods yourself, while grounding annotations in the
actual source pixels and keeping the required visual review loop. Keep all neuron
identity decisions in this conversation; do not delegate frames to other agents.
Preserve input files and leave the tracked project files unchanged.

The task is pixel-level instance segmentation with a body/branch classification
for each neuron. For every included frame, cover all identifiable pixels of each
neuron's cell body (soma) and branches (processes), including thin tips. Both
parts belong to the same persistent neuron ID. Read the adjacent
`annotation_format.md` for mask semantics and `annotations.schema.json` for the
JSON structure. Deliver `annotations.json` conforming to that schema, a copy of
the schema, `tracks.csv`, `annotated.avi`, and `review.json` in the output directory.

1. Inspect the video frames yourself. Find the small dark target features using
   the example JPEG as guidance. View a timeline overview, then inspect useful
   frames and crops at native resolution and pixel-preserving zoom to distinguish
   individual bodies, branch widths, tips, and junctions.
2. Work in one conversation; context compaction is allowed. Keep a saved
   ID/color table, structured annotation file, unresolved identity decisions,
   and review progress so you can continue after compaction or a CLI restart.
   Use one persistent ID for the same target across frames. Never recycle IDs
   for different targets.
3. Choose clear neurons on an anchor frame. Save full-resolution pixel masks for
   each neuron's body and branches. Those masks must be disjoint within a neuron;
   their union is its segmented extent. Trace the full width and visible tips of
   every branch. Rasterize any polygon or brush edits into the saved masks; the
   raster pixels are the annotation used for review and export.
4. Render the saved annotations on the input image. Open and visually inspect
   the actual rendered result, alongside the raw crop. View the body and branch
   layers separately as well as together, with the neuron's persistent color
   and distinct part styling. Check boundaries, missing thin branches, excessive
   mask thickness, background leakage, soma/process classification, and ownership
   at crossings. Correct errors in the mask data, render again, and reopen the
   changed view. Keep the underlying pixels visible during review. Contact
   sheets provide an overview; inspect native-resolution crops for pixel
   boundaries. Record the frame, neuron IDs, crop, revision, and image paths
   actually opened in `review.json`.
5. Carry identities into neighboring frames, using side-by-side crops and
   earlier reviewed appearances as references. Work forward and backward when
   useful. Revisit earlier decisions if later views expose an identity error.
   A changed appearance or a momentary disappearance does not by itself justify
   allocating a new identity.
6. Record `uncertain`, `not_visible`, or `out_of_frame` when appropriate. Do not
   present interpolated or guessed pixels as observed anatomy. Mark uncertain
   ownership and unresolved body/branch boundaries in the uncertainty layer;
   do not silently classify these pixels as background. Do not invent branches
   behind an occlusion. Examine crossings without assuming touching branches
   belong to the same neuron; leave unresolved identity alternatives explicit.
7. Repeat the render, inspect, and correct loop through all frames. If an explicit
   resource limit or context limit prevents completion, save partial results and
   identify the missing coverage. Preserve remaining uncertainty instead of
   silently declaring success at a limit.
8. Before hand-off, validate the machine-readable data, inspect the final
   overlays for every included frame, and inspect temporal comparisons across
   pose changes. Export JSON, CSV, and a colored AVI from exactly that revision.
   Inspect decoded samples of the encoded video too. Recheck affected views
   after any correction; do not hand off changed annotations with stale review
   records.

Use one stable, distinct color and ID label per neuron in the AVI, with different
styling for its body and branches. Preserve frame order and dimensions; check
frame count and playback timing, recording any timing changes in `review.json`.
Validate JSON structure against the supplied schema and check the semantic mask
invariants described in `annotation_format.md`. A successful validation or
rendering command does not replace opening and inspecting the resulting images.

Include source identity, stable ID/color assignments, frame indices, original
timestamps, body/branch/uncertainty masks, visibility, and review state in the
data. Derive tracking positions from body masks. Keep raw media, annotations,
rendered images, video, and session logs under the designated ignored directories.
Do not commit these run artifacts.

The final response must link the machine-readable output, annotated video, and
review record. State which frames and targets were processed, which final
renderings you actually opened, what remains uncertain or incomplete, and
whether the run used compaction or exceeded one context window. Distinguish your
own visual checks from human review. If compaction status cannot be determined,
report it as unknown. Generating an image without viewing it is not a visual
check.
