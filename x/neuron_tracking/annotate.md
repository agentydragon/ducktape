# Neuron annotation prompt

You are the annotator for the supplied microscopy clip. You are responsible for
finding every distinguishable neuron in the requested region, annotating its
visible pixels in every frame, separating body and branches, and maintaining its
identity across the sequence. Deliver the annotations and a colored annotated
video. Choose how to do the work yourself; the result must satisfy the same
requirements regardless of the tools you use.

Use the source clip, target definition, optional reference image, and output
directory supplied in the launch prompt. Follow the requested anatomical region
as the specimen moves and rotates. A reference outline guides target selection;
it is not an already aligned segmentation mask. Continue through the whole clip
without waiting for intermediate approval.

Work in one conversation; context compaction and CLI continuation are allowed.
Keep the ID/color table, annotation revision, unresolved decisions, and review
progress on disk so you can continue reliably. Keep identity decisions in this
conversation; do not delegate frames to other agents. Preserve the inputs and
tracked project files. Keep scratch scripts and generated artifacts in the
assigned output directory outside Git. Use only the supplied inputs and your own
work, without consulting other runs, reference annotations, or evaluation answers.

For every source frame, save full-resolution pixel masks for each neuron's body
(soma), branches (processes), and uncertain pixels under its persistent ID. Cover
the full visible body and the width and tips of visible branches. Both definite
parts belong to the same neuron. Read `annotation_format.md` for mask semantics
and `annotations.schema.json` for the machine format. The saved raster pixels
are the annotations that must be inspected and exported.

You may create tools that help you annotate, inspect, and correct the data, using
the repo's Nix environment, `nix shell`, or `nix-shell` as needed. Use the shared
`x.neuron_tracking.render` helper to display saved masks and export AVI. Run
`python -m x.neuron_tracking.render --help` for its options; its dependencies are
available with `nix-shell -p 'python3.withPackages (ps: [ ps.av ps.pillow ps.pydantic ])'`.

1. Inspect the source sequence yourself, including the entire requested region
   in every frame. Use an overview to orient yourself and sufficiently close,
   pixel-preserving views to distinguish bodies, branch boundaries, and crossings.
   Establish what belongs to the requested population from the supplied inputs.
   You may begin with a clear frame or a few neurons, but that starting point
   does not reduce the scope of the task.
2. Annotate every distinguishable target throughout the sequence. Follow each
   identity between neighboring frames and through changes in pose, focus, and
   appearance. Add neurons that become visible later. Check temporal comparisons
   for identity switches, duplicate IDs for one neuron, and different neurons
   sharing an ID. Revisit earlier frames when later evidence changes a decision.
   Verify disappearance against the source; difficulty matching an identity does
   not establish that a neuron is absent.
3. For every frame, open the saved annotations rendered over its source pixels.
   Compare raw and annotated views over the whole target region, using close
   crops wherever necessary to judge each neuron's pixels. Inspect body and
   branch layers separately and together, with readable IDs, stable distinct
   colors, and the underlying source visible. Check both directions: every
   recognizable target pixel should be accounted for, and every assigned pixel
   and identity should be supported by the source. Look for missed neurons and
   processes as well as incorrect boundaries, background pixels, and ownership.
   Opening a view without assessing those annotations does not complete review.
4. Correct the saved masks and identities wherever review reveals an error or
   omission. Render and open the affected views again. Recheck identity changes
   across all affected frames. Continue this loop until you have addressed the
   discrepancies you can resolve from the source. Checking a few examples does
   not validate the remaining neurons or frames. Contact sheets alone cannot
   establish pixel boundaries. Record the frame, IDs, inspected region, revision,
   actual image paths opened, and resulting corrections or unresolved issues in
   `review.json`.
5. Preserve uncertainty at the place it occurs. An unclear branch assignment
   or boundary does not erase a recognizable body. Distinguishable bodies must
   have separate identities even when some processes cannot be assigned. Never
   use one neuron ID as a container for multiple unresolved neurons. Record
   uncertain identity alternatives explicitly, without claiming an established
   association. Do not invent hidden anatomy or present guesses as observed
   pixels. Use `uncertain`, `not_visible`, and `out_of_frame` according to the
   evidence and the annotation format.
6. Before hand-off, account for every source frame and the entire requested
   population. Each frame needs review of target coverage, pixel ownership,
   body/branch classification, and temporal identity. Having an observation row
   for every frame or a valid JSON file does not satisfy these requirements.
   A few successful examples, an exported video, or a general statement that
   the rest is difficult does not finish the annotation task. Continue working
   on remaining distinguishable targets. If a resource limit or a specific
   ambiguity prevents completion, save the work as partial and identify the
   exact affected frames, regions, IDs, and unresolved decisions. Keep supported
   annotations elsewhere; do not call an incomplete result complete.

Validate the JSON structure and the image-dependent mask invariants in
`annotation_format.md`. Export `annotations.json`, a copy of
`annotations.schema.json`, body-derived `tracks.csv`, `annotated.avi`, and
`review.json` from the same reviewed revision. Each neuron must retain its
color and ID across frames, with distinct body, branch, and uncertainty styling.
Preserve source frame order and dimensions, check decoded frame count and
playback timing, and record timing changes. Inspect decoded AVI samples to check
export fidelity; this is in addition to reviewing annotations for every frame.
Any later annotation change invalidates review of the affected views.

The final response must link the machine-readable outputs, annotated video, and
review record. State annotation and review coverage, remaining localized
uncertainties, and any incomplete work. Distinguish your visual checks from human
review. Report whether compaction occurred, or say unknown if undetermined. Keep
raw media, annotations, renderings, video, and session logs out of Git.
