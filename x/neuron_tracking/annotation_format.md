# Trial annotation format

The saved `annotations.json` conforms to `annotations.schema.json` and contains
full-resolution pixel masks for every included neuron/frame observation. All pixel coordinates refer to the decoded
source image: integer `(x, y)` indexes the pixel in column `x`, row `y`, with
`(0, 0)` at the top left. Frame indices start at zero; timestamps retain the
source presentation times.

Each observation has three masks under the same persistent neuron ID:

- `body`: pixels of the cell body (soma).
- `branches`: pixels of its visible processes, including their full width and
  tips. Individual branches do not need their own persistent identities.
- `uncertain`: candidate pixels whose neuron ownership or body/branch
  classification cannot be resolved. These are excluded from both definite
  masks and must be reported as unresolved.

Within an observation, the masks are pairwise disjoint. The union of `body` and
`branches` is its definite segmented extent; a completed annotation covers every
identifiable neuron pixel, with remaining ambiguity explicitly represented.
Masks for different neurons are independent: preserve uncertain ownership at a
crossing instead of forcing every image pixel to have one confident owner.
Record the alternatives in `review.json`. Do not infer hidden anatomy or require
all branches to form a connected component in the visible image.

Encode each mask losslessly as a list of horizontal pixel runs
`[y, x_start, x_end]`, with an exclusive `x_end`. Thus `[10, 20, 23]` labels
exactly `(20, 10)`, `(21, 10)`, and `(22, 10)`. Runs must have integer bounds,
`0 <= y < height`, and `0 <= x_start < x_end <= width`. Runs within a mask must
not overlap. Sort by row and starting column, and coalesce touching runs within
a mask. An empty mask is `[]`.

The following is a synthetic format example, not an annotation of the sample:

```json
{
  "schema_version": 1,
  "revision": 1,
  "source": {
    "path": "synthetic_clip.avi",
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "width": 64,
    "height": 48
  },
  "frames": [{ "frame_index": 0, "timestamp_s": 0.0 }],
  "neurons": [{ "id": "N001", "color": "#e6194b" }],
  "observations": [
    {
      "neuron_id": "N001",
      "frame_index": 0,
      "visibility": "visible",
      "body": [
        [10, 20, 23],
        [11, 20, 23]
      ],
      "branches": [[10, 23, 27]],
      "uncertain": [[10, 27, 28]],
      "review": "proposed"
    }
  ]
}
```

An observation's `visibility` is `visible`, `uncertain`, `not_visible`, or
`out_of_frame`; `review` is `proposed`, `agent_reviewed`, or `human_reviewed`.
Visibility describes the observation as a whole; the uncertainty mask describes
specific ambiguous pixels even when other parts are visible. `not_visible` and
`out_of_frame` observations have empty masks. A body can be absent while branches
remain visible. Keep one observation per neuron per included frame, including
explicit absence states.

Render directly from these saved runs. Use the neuron's color for both parts,
distinguishing body and branches with fill/outline or opacity styling, and give
uncertainty a separate visible style. Review raw, combined, body-only, and
branch-only views; zoom without interpolating mask labels. Edit by tracing,
brushes, polygons, or computed proposals as useful, then save and inspect the
resulting pixel masks. The model need not enumerate every foreground pixel in
its textual response.

JSON Schema checks structure; separately validate the image-dependent run bounds,
mask disjointness, unique neuron/frame observations, complete frame coverage,
and frame/ID references. A schema-valid file can still contain incorrect masks.

Derive `tracks.csv` positions from body-mask centroids and include body/branch
pixel areas, frame index, timestamp, ID, visibility, and review state. If the body mask is
empty, its centroid is null. JSON masks remain the source of truth for pixel
annotations; the CSV is a summary. `review.json` identifies the exact saved
revision whose overlays were opened, unresolved pixel/identity alternatives,
and incomplete frame or neuron coverage. Changing masks invalidates affected
review records.
