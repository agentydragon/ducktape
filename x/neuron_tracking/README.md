# Neuron tracking experiment

Explore using one Codex conversation to segment every visible pixel of each
neuron in microscopy video, separate its body and branches, preserve its identity
across frames, and inspect and correct its rendered masks before hand-off.

The annotation prompt is in `annotate.md`, with mask semantics in
`annotation_format.md` and the machine-readable format in
`annotations.schema.json`. Five independent `gpt-6-luna` / `xhigh` trials have
finished; their artifacts and native traces are under
`~/drive/neuron-tracking/runs/`.
All produced partial annotations; see [the trial review](debug/first_trials.md).
Codex may use Nix and write its own processing scripts. Follow-up work is in
`TODO.md`.

Keep each run in one conversation. Context compaction is allowed; save identity
decisions, annotation revisions, and review progress on disk for continuation.

Keep input media in `~/drive/neuron-tracking/data/` and generated frames,
annotations, overlays, videos, and session logs in
`~/drive/neuron-tracking/runs/`. These are actual files in the synced Drive folder,
outside the Git checkout. Do not create checkout-local copies or links to them.
The Drive folder's `README.md` describes the saved runs, source snapshot, and
continuation steps. Do not commit media or run artifacts.

## First run

Run from the repository root, using the source clip and reference JPEG in
`~/drive/neuron-tracking/data/`:

```bash
neuron_run="$HOME/drive/neuron-tracking/runs/luna_xhigh"
mkdir -p "$neuron_run"
codex exec --model gpt-6-luna -c 'model_reasoning_effort="xhigh"' \
  --approve-for-me --json \
  --output-last-message "$neuron_run/handoff.md" \
  "Read x/neuron_tracking/annotate.md and x/neuron_tracking/sample_task.md. Process all frames from $HOME/drive/neuron-tracking/data/IMG_6128.AVI, using $HOME/drive/neuron-tracking/data/photo_2026-10-19_14-30-28.jpg as the target reference. Save the colored annotated AVI, pixel annotations, schema, tracking CSV, and review record under $neuron_run." \
  > "$neuron_run/codex_events.jsonl"
```

Use a fresh output directory for each run. This uses the existing Codex login
and provider, with the selected model and effort set for this invocation only.
Long runs need a durable process supervisor when their invoking agent's process
lifetime is uncertain; unexpected SIGTERM exits occurred during the first trials.
See [the abort investigation](debug/parallel_run_aborts.md) for the observed
mitigation and the limits of the diagnosis.

## Shared renderer

The helper reads source video and `annotations.json`; it does not detect neurons
or change masks. Sample-specific target constraints stay in `sample_task.md`.
Each ID keeps its JSON color across frames and all mask layers. Body pixels use
a translucent fill, branches a stronger fill, and uncertainty a striped fill in
that same color. Labels show the ID with its color border and leader line.

From the repository root, enter a Nix shell with the renderer's dependencies:

```bash
nix-shell -p 'python3.withPackages (ps: [ ps.av ps.pillow ps.pydantic ])'
```

Then render a single frame or the full AVI:

```bash
python -m x.neuron_tracking.render \
  --source "$HOME/drive/neuron-tracking/data/IMG_6128.AVI" \
  --annotations "$HOME/drive/neuron-tracking/runs/luna_xhigh_03/annotations.json" \
  --frame 68 --output "$HOME/drive/neuron-tracking/runs/renderer/frame_068.png"

python -m x.neuron_tracking.render \
  --source "$HOME/drive/neuron-tracking/data/IMG_6128.AVI" \
  --annotations "$HOME/drive/neuron-tracking/runs/luna_xhigh_03/annotations.json" \
  --output "$HOME/drive/neuron-tracking/runs/renderer/annotated.avi"
```

Use `--layer body`, `--layer branches`, or `--layer uncertain` for separate
views. Repeat `--neuron ID` to select identities; `--no-labels` leaves pixel
boundaries unobscured. Masks from different neurons draw in ID order; use
individual-ID views to inspect overlapping ownership. Sparse annotations can
render an anchor frame; full-video export requires every source frame.

The helper checks source hash, dimensions, timestamps, colors, references, and
mask invariants. Every output gets a `.png.json` or `.avi.json` report recording
the annotation hash/revision, palette, selected frames/layer, and output hash.
AVI uses lossless FFV1 with exactly one encoded frame per source frame and a
constant cadence based on source duration. The helper decodes the result to
check frame count and dimensions. Its report keeps original and output
timestamps and both durations so timing changes are explicit. Rendering is not
evidence of correct neuron selection or tracking; open and inspect the outputs.

## Sample sanity checks

These are evaluator checks. For a blind prompt comparison, provide the annotation
agent only its instructions, input media, target definition, format/schema, and
renderer access. Keep these criteria, the reviewed annotations, and prior runs
outside its input workspace; evaluate its saved output afterward. The
`sample_task.md` contains only the sample's anatomical target definition.

Run the checks against an annotation JSON from the repository root, in the Nix
shell above (the checker itself only needs Pydantic):

```bash
python -m x.neuron_tracking.check \
  --annotations "$HOME/drive/neuron-tracking/runs/manual_frame_review/annotations.json" \
  --checks x/neuron_tracking/sample_checks.json
```

`sample_checks.json` pins this source by SHA-256 and dimensions and selects
frame 52 at 25.9 seconds. Its named checks require:

- 12–20 predicted body centers inside `[245, 560, 635, 805]`, the reviewed
  side/belly crop.
- At least one predicted body center inside `[332, 714, 348, 731]`, around the
  blue example's soma.
- Body-pixel overlap with each of these boxes, covering the full body, branch,
  and uncertainty mask extent of the corresponding reviewed candidate:

  | Example                 | Box                    |
  | ----------------------- | ---------------------- |
  | Dark blue               | `[487, 643, 519, 669]` |
  | Dark teal just below it | `[493, 676, 518, 693]` |
  | Leftmost red/pink       | `[254, 758, 290, 786]` |

- All four examples must be present and have separate identities: no predicted
  neuron ID may match more than one of their boxes.

Regions use source pixels as `[left, top, right, bottom]`, excluding the right
and bottom edges. `body_center` uses the area-weighted centroid of the saved
`body` pixels. `body_overlap` requires at least one body pixel in the box, even
if its center lies outside. Each ID counts once; branch and uncertainty pixels
do not satisfy either predicate. Colors, ID names, and review labels do not
determine a location match. Distinctness compares the matched IDs and also fails
if an example is missing. The checker prints JSON with pass/fail, matching IDs,
and IDs shared between examples, exits 0 when all checks pass, and exits 1 for
failed checks. Wrong source/frame metadata raises an input error.

Add named regions to this file or create another sample/frame check file as
examples accumulate, and add named `distinct` groups for examples that must
have separate IDs. A null `max_count` leaves the upper bound open. These are
coarse checks: passing does not establish mask boundaries or correct tracking.
None of the first five runs pass all checks; see
[the baseline comparison](debug/sanity_check_baseline.md).
