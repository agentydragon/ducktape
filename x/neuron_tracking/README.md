# Neuron tracking experiment

Explore using one Codex conversation to segment every visible pixel of each
neuron in microscopy video, separate its body and branches, preserve its identity
across frames, and inspect and correct its rendered masks before hand-off.

The annotation prompt is in `annotate.md`, with mask semantics in
`annotation_format.md` and the machine-readable format in
`annotations.schema.json`. Five independent `gpt-6-luna` / `xhigh` trials have
finished; their local artifacts and native traces are under `runs/`.
All produced partial annotations; see [the trial review](debug/first_trials.md).
Codex may use Nix and write its own processing scripts. Follow-up work is in
`TODO.md`.

Keep each run in one conversation. Context compaction is allowed; save identity
decisions, annotation revisions, and review progress on disk for continuation.

Keep local input media in `data/` and generated frames, annotations, overlays,
videos, and session logs in `runs/`. Both directories and common media extensions
are ignored. Source files can also remain outside the checkout. Do not commit
media blobs or local symlinks to them.

## First run

From the repository root, put the source clip and reference JPEG in `data/` or
pass absolute paths to files outside the checkout. With files under `data/`, run:

```bash
mkdir -p x/neuron_tracking/runs/luna_xhigh
codex exec --model gpt-6-luna -c 'model_reasoning_effort="xhigh"' \
  --approve-for-me --json \
  --output-last-message x/neuron_tracking/runs/luna_xhigh/handoff.md \
  'Read x/neuron_tracking/annotate.md and x/neuron_tracking/sample_task.md. Process all frames from x/neuron_tracking/data/IMG_6128.AVI, using x/neuron_tracking/data/photo_2026-10-19_14-30-28.jpg as the target reference. Save the colored annotated AVI, pixel annotations, schema, tracking CSV, and review record under x/neuron_tracking/runs/luna_xhigh.' \
  > x/neuron_tracking/runs/luna_xhigh/codex_events.jsonl
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
  --source x/neuron_tracking/data/IMG_6128.AVI \
  --annotations x/neuron_tracking/runs/luna_xhigh_03/annotations.json \
  --frame 68 --output x/neuron_tracking/runs/renderer/frame_068.png

python -m x.neuron_tracking.render \
  --source x/neuron_tracking/data/IMG_6128.AVI \
  --annotations x/neuron_tracking/runs/luna_xhigh_03/annotations.json \
  --output x/neuron_tracking/runs/renderer/annotated.avi
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
