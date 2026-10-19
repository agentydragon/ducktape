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
  'Read x/neuron_tracking/annotate.md. Process all frames from x/neuron_tracking/data/IMG_6128.AVI, using x/neuron_tracking/data/photo_2026-10-19_14-30-28.jpg as the target reference. Save the colored annotated AVI, pixel annotations, schema, tracking CSV, and review record under x/neuron_tracking/runs/luna_xhigh.' \
  > x/neuron_tracking/runs/luna_xhigh/codex_events.jsonl
```

Use a fresh output directory for each run. This uses the existing Codex login
and provider, with the selected model and effort set for this invocation only.
Long runs need a durable process supervisor when their invoking agent's process
lifetime is uncertain; unexpected SIGTERM exits occurred during the first trials.
See [the abort investigation](debug/parallel_run_aborts.md) for the observed
mitigation and the limits of the diagnosis.
