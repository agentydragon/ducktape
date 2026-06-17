# Debundle CLI basics

Setup, output formats, the pipeline, binding inspection, and the gate — the
common operational base for every debundle workflow. The command surface itself
is in `cli.md`.

## Setting common env vars once

Every read-only and mutating command accepts `--graph`, `--modules`,
and (for source-reading commands) `--source-root`. Export the
corresponding env vars once per shell session and subsequent commands
run without repeating the flags:

```bash
export DEBUNDLE_GRAPH=<debundle-output>/reports/tree/<chunk-id>/owner_graph.json
export DEBUNDLE_MODULES=<spec-root>/<version>/modules
export DEBUNDLE_SOURCE_ROOT=<debundle-output>/app
export DEBUNDLE_OUT=<debundle-output-root>
```

Flags win when both are set. Use this for one-off overrides.

`DEBUNDLE_SOURCE_ROOT` feeds only the query commands' `--source-root`
(the upstream snapshot root). `debundle run --tree-source-root` — the
spec-tree compile root — reads the separate `DEBUNDLE_TREE_SOURCE_ROOT`,
since the two roots are different directories in real corpora.

If remote execution downloads only minimal outputs, request full outputs
so side files are local:

```bash
--remote_download_outputs=all
```

## Output formats

Read-only commands accept `--format text|json|ndjson`:

- `text` — interactive default, scannable on a terminal.
- `json` — single JSON document, parseable with `jq`.
- `ndjson` — one JSON value per line, for streaming consumers.

If `--format` isn't passed and stdout is **not** a tty, the default
flips to `json`. So `debundle modules propose | jq ...` works without
an explicit `--format json`.

Reach for `ndjson` on streaming queries with many rows
(`debundle scc --format ndjson` over a large graph), or when piping
into `jq -c` / `xargs`.

Mutating commands (`bindings assign`, `bindings rename`, `modules
merge`) print a one-line verdict (`ok`, `would change N files`,
`rejected ...`). `--dry-run` adds the planned diff summary; a
structured YAML diff is not yet in v1.

## Running the pipeline

Run the transform pipeline:

```bash
debundle run --spec <transform-spec.yaml>

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir>
```

The gate is part of the pipeline contract: if the spec is
unrealizable, `debundle run` rejects and emits structured side outputs
(`cycles.json`, `atomic_unit_conflicts.json`) under
`reports/tree/<chunk-id>/`. There is no `run --no-verify` — fix the
spec first.

Use `debundle run --dry-run` to run pipeline parse/facts/gate checks
without writing emitted JS or reports.

Broad spec migrations continue through supported diagnostic failures by
default and report all findings from that pass. Today this aggregates
unresolved source-match selectors and duplicate binding claims during
materialization plan building, including the chunk, binding, existing
owner/export/origin, and competing owner/export/origin. Use `--fail-fast` only
when the first failing selector or claim is the useful debugging target.

## Workflow: investigating a binding end-to-end

When a binding's role is unclear or a proposal is suspicious:

1. **`debundle describe <sym>`** — graph + spec context. `<sym>` is
   either the minified name (`XOe`) or the readable name
   (`PluginSettingsAccessor`). Output includes the binding's owner,
   home module, atom membership, and incoming/outgoing edges. Add
   `--include-proposals` only when factorizer proposal/diagnostic
   annotations are needed.
2. **`debundle show-source <sym>`** — print the original source span
   for the owner. Use `--context-lines 40` to widen the view.
3. **`debundle cluster <sym>`** — list the module-quotient neighbors
   of the binding's owner. Useful for "what does this module touch?"
   questions before deciding a destination.

`describe` and `show-source` accept any ID kind: bindings, module
paths (`runtime/plugins`), owner IDs (`owner:42`), atom IDs, proposal
IDs, diagnostic IDs. The renderer dispatches on the kind it detects.

## Evidence files

Typical debundle outputs include:

- executable JS under `app/`
- root reports under `reports/`: `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, `vendor_swaps.json` when those outputs are configured
- per-chunk reports under `reports/tree/<chunk-id>/`: `chunk.json`,
  `modules.json`, `owner_graph.json`
- `reports/tree/<chunk-id>/cycles.json` or
  `reports/tree/<chunk-id>/atomic_unit_conflicts.json` only when
  validation rejects
- mirrored per-directory and per-file dependency reports under
  `reports/tree/**/index.json` and `reports/tree/**/*.js.json`

Use manifests for progress reporting rather than rescanning generated
JS by hand. Use tree reports for hierarchy-health evidence:
incoming/outgoing semantic dependency counts by kind, and full
symbol/file attribution for boundary crossings that make a directory
leaky or well-encapsulated. Treat them as graph evidence to pair with
source reading, not as a substitute for understanding the
implementation.

## Gate discipline

The adapter-provided gate is authoritative. For suspicious green
builds, force a fresh execution using whatever mechanism the project
build supports. Do not trust cache-only success when validating new
module boundaries.

Generated JS conflicts should be resolved by the adapter regen
command, not by hand-editing generated output.
