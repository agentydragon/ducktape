# Debundle User Guide

This guide describes the shared `debundle` command surface. The binary has
two halves:

- **Pipeline**: `debundle run` executes the transform pipeline from a flat
  or tree-shaped spec. Heavyweight target; Bazel drives it through
  `debundle_pipeline_with_profiles`.
- **Queries / spec edits**: a small orthogonal set of subcommands
  (`binding`, `scc`, `cluster`, `peel`) that read the pipeline's emitted
  reports under `<chunk_id>/owner_graph.json` and the spec tree under
  `<modules_root>/<path>/<file>.yaml`. They never touch the JS pipeline.

For deeper detail on the planning-only `peel` surface see the
`debundle_plan_work` skill.

## Design Principles (Query / Edit Surface)

1. **Orthogonal subcommand groups** reflect concepts (a binding, a
   strongly-connected component, a module cluster), not pipeline
   internals. The legacy `peel` group is preserved for backwards
   compatibility but new work should sit under top-level `binding` /
   `scc` / `cluster`.
2. **JSON-first output.** Every query subcommand emits JSON to stdout,
   suitable for `jq`. Listing commands take `--ndjson` for one record per
   line. No human-only output modes.
3. **No subcommands for things already trivial in shell.** Disabling a
   YAML is `mv foo.yaml foo.yaml.disabled`; renaming a module is `mv`;
   finding all bindings in a directory is
   `grep -r '  - name:' spec/modules/foo`. The CLI sticks to operations
   that need the owner graph or the spec's deserialized shape.
4. **Edit commands write the spec and exit.** Regen is the user's
   responsibility (`bazelisk run //tana/re/web/<version>:regen_js`). That
   keeps the CLI fast and composable.

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
debundle run --spec <transform-spec.yaml>

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir>
```

## Binding Queries and Edits

### `debundle binding describe <name>`

Print a JSON record for one binding: current spec home (which YAML owns
it, or `null` if residual), source location, owner ids, declared
statement shape, atomic-unit membership, and current destination module
ref.

```bash
debundle binding describe \
    --graph "$GRAPH" --modules "$MODULES" XOe
```

### `debundle binding show-code <name>`

Print the source body of the owner statement(s) that declare the
binding. Useful for "what does this minified symbol actually do?" without
reading the whole chunk. Pass `--source-root <dir>` to resolve
chunk-relative paths.

```bash
debundle binding show-code \
    --graph "$GRAPH" --modules "$MODULES" \
    --source-root "$SOURCE_ROOT" \
    --context-lines 5 XOe
```

### `debundle binding assign <name> <module-path>`

Move a binding into a specific module's YAML. Creates the YAML if
missing, removes the binding from its previous home (and drops the old
YAML if it becomes empty). Pass `--rename <export-name>` to set the
export name; otherwise the binding name is used. `--dry-run` prints the
planned write without modifying disk. Pass `--graph "$GRAPH"` to enable
the same realizability check `binding move` runs (off by default for
backward compatibility); `--force` bypasses it.

```bash
debundle binding assign \
    --modules "$MODULES" \
    --rename PluginSettingsAccessor \
    XOe runtime/plugins
bazelisk run //tana/re/web/78d928dca7:regen_js
```

### `debundle binding unassign <name>`

Inverse of `assign`. Drop the binding from its module so it falls back
into residual on the next pipeline run. Single-op alias for
`binding move <name>=-`.

### `debundle binding move <ops>...`

The batched verb. Subsumes `assign`/`unassign` and lets cycle-aware
multi-move plans land atomically: the full set of moves is applied to
an in-memory spec copy, the realizability check runs over the final
state, and _all_ moves write only if it passes. If validation fails,
no file is touched and the diagnostic is printed to stderr.

Single-op shape (backward compatible with `assign`):

```bash
debundle binding move \
    --modules "$MODULES" \
    XOe runtime/plugins
```

Batch shapes (pick whichever is most ergonomic):

```bash
# Positional pairs (terse for one-liners):
debundle binding move \
    --graph "$GRAPH" --modules "$MODULES" \
    X=runtime/plugins Y=runtime/plugins Z=runtime/utils

# Repeated --op flags (handy in scripts that build args up):
debundle binding move \
    --graph "$GRAPH" --modules "$MODULES" \
    --op X=runtime/plugins \
    --op Y=runtime/plugins

# Batch file (one `name=destination` per line; `#` comments allowed):
debundle binding move \
    --graph "$GRAPH" --modules "$MODULES" \
    --batch ops.txt
```

Sentinel destinations:

- `name=-` (or `name=residual`, `name=<residual>`) — unassign the
  binding back to residual.

Output: per-op `ok    NAME -> DEST` lines on stdout, then a trailing
`N ops applied.` summary. With `--ndjson`, each op is emitted as a
JSON record on its own line, followed by a JSON summary record.

Flags:

- `--graph <owner_graph.json>` enables realizability validation.
  Without it the batch lands unchecked (intended for pure-rename
  workflows that don't have a graph on hand).
- `--dry-run` runs validation but writes nothing.
- `--force` bypasses the realizability check (duplicate-destination
  and unresolved-binding checks still run).
- `--ndjson` swaps the per-op output to JSON-per-line.

The killer use case is the cycle-aware batch: moving binding `X`
alone might create a module-quotient cycle, but moving `{X, Y}`
together to the same module collapses the back edge into a
self-edge and the batch lands cleanly. A wrapper script is no
longer needed — `debundle binding move X=foo Y=foo` is one command.

## SCC Queries

### `debundle scc [filters]`

List strongly-connected components in the module-quotient graph. Default
output is a pretty JSON array; with `--ndjson`, one record per line.

Filters compose:

- `--module <path>` — restrict to the SCC containing the named module.
- `--binding <name>` — same but resolved via `--modules`.
- `--min-size <n>` / `--max-size <n>` — size predicates.
- `--cycles-only` — `size >= 2 && is_cycle`.
- `--singletons-only` — `size == 1`. Mutually exclusive with
  `--cycles-only`.
- `--residual-only` — every member module is under a `residual/` path
  (i.e. a clean extraction candidate not yet promoted).

```bash
# Every cycle in the current pipeline output, one per line.
debundle scc --graph "$GRAPH" --cycles-only --ndjson

# Singleton SCCs that live inside residual — extraction candidates.
debundle scc --graph "$GRAPH" \
    --singletons-only --residual-only --ndjson \
    | jq -c '{id, modules}'
```

## Cluster Queries

### `debundle cluster --module <path> | --binding <name>`

List the 1-hop neighbors of a module in the quotient graph. Each record
carries `direction` (`inbound` / `outbound` / `both`), `edge_count`, and
a `constrains_init_order` bit.

```bash
debundle cluster \
    --graph "$GRAPH" --modules "$MODULES" \
    --binding XOe --ndjson
```

## Peel Queries (Planning Surface)

Use `debundle peel` for factorizer-specific proposals and diagnostics.
The orthogonal `binding` / `scc` / `cluster` commands subsume most
everyday use; reach for `peel` when planning bulk module-assignment
work.

```bash
debundle peel plan-work --graph "$GRAPH" --modules "$MODULES" --limit 25
debundle peel patch-plan --graph "$GRAPH" --modules "$MODULES" --limit 50
debundle peel units --graph "$GRAPH" --modules "$MODULES" --readable-only --limit 100
debundle peel graph-summary --graph "$GRAPH" --modules "$MODULES" --limit 25
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --proposal-id <id>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --unit-id <id>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --binding-id <binding>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --owner-id <owner>
debundle peel source-slice --graph "$GRAPH" --modules "$MODULES" \
  --proposal-id <id> --source-root "$SOURCE_ROOT" --context-lines 40
```

`explain` and `source-slice` select exactly one object with
`--proposal-id`, `--unit-id`, `--diagnostic-id`, `--owner-id`, or
`--binding-id`; there is no `--binding` shorthand.

Interpretation:

- `plan-work` is the bulk module-assignment proposal queue.
- `patch-plan` describes whether current module YAML and binding-patch
  sets cover whole atomic units, split units, or unknown bindings.
- `units` is the atomic-DAG unit catalog.
- `graph-summary` is the aggregate DAG/proposal overview.
- `explain` is the graph/spec drill-down.
- `source-slice` is the source-reading primitive for planner objects.

## Recipes

### "Which SCC contains this binding?"

```bash
debundle scc --graph "$GRAPH" --modules "$MODULES" --binding XOe
```

### "What size-1 SCCs are inside residual?"

```bash
debundle scc --graph "$GRAPH" \
    --singletons-only --residual-only --ndjson \
    | jq -c '{id, modules}'
```

### "Show me the source for this binding"

```bash
debundle binding show-code \
    --graph "$GRAPH" --modules "$MODULES" \
    --source-root "$SOURCE_ROOT" \
    XOe | jq -r '.slices[0].text'
```

### "Move a binding into a module, then regen"

```bash
debundle binding assign --modules "$MODULES" XOe runtime/plugins
bazelisk run //tana/re/web/<version>:regen_js
```

### "Move a cycle-aware batch of bindings together"

When a single move would create a cycle but a batched move closes it
(e.g. moving `X` alone leaves `X -> Y` and `Y -> X` straddling
modules; moving `{X, Y}` together collapses both onto the new
module's self-edge):

```bash
debundle binding move \
    --graph "$GRAPH" --modules "$MODULES" \
    X=runtime/plugins Y=runtime/plugins
bazelisk run //tana/re/web/<version>:regen_js
```

If the batch would itself create a cycle, the command exits
nonzero, writes nothing, and prints the cycle's modules and the
heaviest cut edges. Re-run with `--force` only when you have
already understood the cycle and intend to commit anyway.

### "Try a hypothetical spec edit and see what changes"

A shell recipe, not a CLI flag. Save the spec, edit, rebuild, read the
SCC report, revert if bad.

```bash
cp -r "$MODULES" /tmp/modules.before
debundle binding assign --modules "$MODULES" XOe runtime/plugins
bazelisk build //tana/re/web/<version>:debundle --config=rbe \
    --remote_upload_local_results=false
debundle scc --graph "$GRAPH" --cycles-only --ndjson | wc -l
rm -rf "$MODULES" && mv /tmp/modules.before "$MODULES"
```

### "Rename a module" / "Disable a YAML"

Both are plain `mv`. The spec compiler infers module path from file
location and ignores files that don't end in `.yaml`.

```bash
mv "$MODULES"/runtime/plugins.yaml "$MODULES"/runtime/plugin_settings.yaml
mv "$MODULES"/runtime/plugins.yaml "$MODULES"/runtime/plugins.yaml.disabled
```

## Evidence Files

Typical debundle outputs include:

- executable JS under `app/`
- root reports under `reports/`, including `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, and `vendor_swaps.json` when those stages run
- per-chunk reports under `reports/tree/<chunk-id>/`, including
  `chunk.json`, `modules.json`, and `owner_graph.json`
- `reports/tree/<chunk-id>/cycles.json` or
  `reports/tree/<chunk-id>/atomic_unit_conflicts.json` only when
  validation rejects
- mirrored per-directory and per-file dependency reports under
  `reports/tree/**/index.json` and `reports/tree/**/*.js.json`

Use manifests for progress reporting rather than rescanning generated JS
by hand. Use tree reports for hierarchy-health evidence:
incoming/outgoing semantic dependency counts by kind, and full
symbol/file attribution for the boundary crossings that make a directory
leaky or well-encapsulated. Treat them as graph evidence to pair with
source reading, not as a substitute for understanding the
implementation.

## Gate Discipline

The adapter-provided gate is authoritative. For suspicious green builds,
force a fresh execution using whatever mechanism the project build
supports. Do not trust cache-only success when validating new module
boundaries.

Generated JS conflicts should be resolved by the adapter regen command,
not by hand-editing generated output.
