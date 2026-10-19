# Draft annotation prompt

This prompt is for the first supervised experiment. The helper commands and
end-to-end workflow still need to be established; do not treat this document as
a tested skill or claim completed tracking when required helpers are missing.

Given an input clip, a target region or example image, an output directory, and
an agreed trial scope:

1. Inspect the video frames yourself. Find the small dark target features using
   the example JPEG as guidance. View a timeline overview, then inspect useful
   frames and crops at sufficient resolution to place individual points.
2. Work in one conversation, preferably within one context window. Keep a saved
   ID/color table and structured annotation file as you go. Use one persistent
   ID for the same target across frames. Never recycle IDs for different targets.
3. Choose clear features on an anchor frame. Save original-frame center
   coordinates and display radii, following the established output schema.
   State uncertainty when adjacent features cannot be separated reliably.
4. Render the saved annotations on the input image. Open and visually inspect
   the actual rendered result, alongside the raw crop. Check every proposed
   point for placement, omissions, duplicates, and identity mistakes. Correct
   errors in the annotation data, render again, and reopen the changed view.
5. Carry identities into neighboring frames, using side-by-side crops and
   earlier reviewed appearances as references. Work forward and backward when
   useful. Revisit earlier decisions if later views expose an identity error.
   A changed appearance or a momentary disappearance does not by itself justify
   allocating a new identity.
6. Record `uncertain`, `not_visible`, or `out_of_frame` when appropriate. Do not
   present interpolated or guessed coordinates as observed positions. Leave
   unresolved identity alternatives explicit.
7. Repeat the render, inspect, and correct loop until the trial scope is
   reviewed or the agreed iteration/context/token limit is reached. Preserve
   remaining uncertainty instead of silently declaring success at a limit.
8. Before hand-off, validate the machine-readable data, inspect the final
   overlays for every included frame, and inspect temporal comparisons across
   pose changes. Export JSON, CSV, and a video from exactly that revision.
   Inspect decoded samples of the encoded video too. Recheck affected views
   after any correction; do not hand off changed annotations with stale review
   records.

Include source identity, coordinate convention, stable ID/color assignments,
frame indices, original timestamps, positions, visibility, and review state in
the data. Keep raw media, annotations, rendered images, video, and session logs
under the designated ignored directories. Do not commit these run artifacts.

The final response must link the machine-readable output, annotated video, and
review record. State which frames and targets were processed, which final
renderings you actually opened, what remains uncertain or incomplete, and
whether the run used compaction or exceeded one context window. Distinguish your
own visual checks from human review. Generating an image without viewing it is
not a visual check.
