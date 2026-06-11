# Debundle User Guide

This guide describes the shared `debundle` command surface.

- `debundle run ...` executes the transform pipeline from a flat or
  tree-shaped spec.
- `debundle peel ...` runs read-only graph/spec/source queries for AI
  workflow roles planning module peel work.

Use this with `debundle_plan_work`, which is the detailed command guide for
`debundle peel`.

## Common Inputs

Adapters should bind these names before invoking role skills:

```bash
GRAPH=<debundle-output>/reports/tree/<chunk-id>/owner_graph.json
MODULES=<spec-root>/<version>/modules
SOURCE_ROOT=<debundle-output>/app
DEBUNDLE_OUT=<debundle-output-root>
REPORT_TREE=<debundle-output-root>/reports/tree
```

If remote execution or minimal output downloads are in use, request full
outputs so side files are local:

```bash
--remote_download_outputs=all
```

## Transform Mode

Run the transform pipeline:

```bash
debundle run --spec <transform-spec.yaml> --force

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir> \
  --force
```

## Peel Queries

Use `debundle peel` for graph/source evidence:

```bash
debundle peel plan-work --graph "$GRAPH" --modules "$MODULES" --limit 25
debundle peel patch-status --graph "$GRAPH" --modules "$MODULES" --limit 50
debundle peel candidates --graph "$GRAPH" --modules "$MODULES" --limit 100
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --proposal-id <id>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --binding-id <binding>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --owner-id <owner>
debundle peel source-slice --graph "$GRAPH" --modules "$MODULES" \
  --proposal-id <id> --source-root "$SOURCE_ROOT" --context-lines 40
```

`explain` and `source-slice` select exactly one object with `--proposal-id`,
`--owner-id`, or `--binding-id`; there is no `--binding` shorthand.

Interpretation:

- `plan-work` is the bulk module-assignment proposal queue.
- `patch-status` describes non-emitting patch-stream assignability.
- `candidates` is a symbol-level catalog.
- `explain` is the graph/spec drill-down.
- `source-slice` is the source-reading primitive for planner objects.

## Evidence Files

Typical debundle outputs include:

- executable JS under `app/`
- root reports under `reports/`, including `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, and `vendor_swaps.json` when those stages run
- per-chunk reports under `reports/tree/<chunk-id>/`, including
  `chunk.json`, `modules.json`, and `owner_graph.json`
- `reports/tree/<chunk-id>/cycles.json` or
  `reports/tree/<chunk-id>/atomic_unit_conflicts.json` only when validation
  rejects
- mirrored per-directory and per-file dependency reports under
  `reports/tree/**/index.json` and `reports/tree/**/*.js.json`

Use manifests for progress reporting rather than rescanning generated JS by
hand. Use tree reports for hierarchy-health evidence: incoming/outgoing
semantic dependency counts by kind, and full symbol/file attribution for the
boundary crossings that make a directory leaky or well-encapsulated. Treat
them as graph evidence to pair with source reading, not as a substitute for
understanding the implementation.

## Gate Discipline

The adapter-provided gate is authoritative. For suspicious green builds,
force a fresh execution using whatever mechanism the project build supports.
Do not trust cache-only success when validating new module boundaries.

Generated JS conflicts should be resolved by the adapter regen command, not by
hand-editing generated output.
