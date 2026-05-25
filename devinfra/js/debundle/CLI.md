# `debundle` CLI surface

The `debundle` binary has two halves:

- **Pipeline**: `debundle run` executes the transform pipeline from
  a flat or tree-shaped spec. This is the heavyweight target — Bazel
  drives it through `debundle_pipeline_with_profiles`.
- **Queries / spec edits**: a small orthogonal set of subcommands
  (`binding`, `scc`, `cluster`, `peel`) that read the pipeline's
  emitted reports under `<chunk_id>/owner_graph.json` and the spec
  tree under `<modules_root>/<path>/<file>.yaml`. They never touch
  the JS pipeline itself.

This doc covers the second half — the canonical surface for spec
authoring / agent tooling. The pipeline surface is documented in
`README.md` and `DESIGN.md`.

## Design principles

1. **Orthogonal subcommand groups** reflect concepts (a binding, a
   strongly-connected component, a module cluster), not pipeline
   internals. The legacy `peel` subcommand group is preserved for
   backwards compatibility but new work should sit under the
   top-level `binding` / `scc` / `cluster` commands.
2. **JSON-first output.** Every query subcommand emits JSON to
   stdout, suitable for `jq`. Listing commands take `--ndjson` for
   one record per line so the stream is pipeable into
   `jq -c 'select(...)'`. No human-only output modes.
3. **No subcommands for things that are already trivial shell.**
   Disabling a YAML is `mv foo.yaml foo.yaml.disabled`; renaming a
   module is `mv`; finding all bindings in a directory is `grep -r
   '  - name:' spec/modules/foo`. The CLI sticks to operations that
   need the owner graph or the spec's deserialized shape.
4. **Edit commands write the spec and exit.** Regen is the user's
   responsibility (`bazelisk run //tana/re/web/<version>:regen_js`).
   That keeps the CLI fast and composable.

## Reference

### `debundle binding describe <name>`

Print a JSON record for one binding: current spec home (which YAML
owns it, or `null` if it's in residual), source location, owner ids,
declared statement shape, atomic-unit membership, and current
destination module ref.

```bash
debundle binding describe \
    --graph reports/tree/static/index-DI2GynTv/owner_graph.json \
    --modules tana/re/web/78d928dca7/spec/modules \
    XOe
```

### `debundle binding show-code <name>`

Print the source body of the owner statement(s) that declare the
binding. Useful for "what does this minified symbol actually do?"
without reading the whole chunk. Pass `--source-root <dir>` to
resolve chunk-relative paths.

```bash
debundle binding show-code \
    --graph reports/tree/static/index-DI2GynTv/owner_graph.json \
    --modules tana/re/web/78d928dca7/spec/modules \
    --source-root tana/upstream/web/snapshots/78d928dca7 \
    --context-lines 5 \
    XOe
```

### `debundle binding assign <name> <module-path>`

Move a binding into a specific module's YAML. Creates the YAML if
missing, removes the binding from its previous home (and drops the
old YAML if it becomes empty). Pass `--rename <export-name>` to set
the export name; otherwise the binding name is used. `--dry-run`
prints the planned write without modifying disk.

```bash
debundle binding assign \
    --modules tana/re/web/78d928dca7/spec/modules \
    --rename PluginSettingsAccessor \
    XOe \
    runtime/plugins
# Then regen:
bazelisk run //tana/re/web/78d928dca7:regen_js
```

### `debundle binding unassign <name>`

Inverse of `assign`. Drop the binding from its module so it falls
back into residual on the next pipeline run.

### `debundle scc [filters]`

List strongly-connected components in the module-quotient graph.
Default output is a pretty JSON array; with `--ndjson`, one record
per line.

Filters compose:

- `--module <path>` — restrict to the SCC containing the named
  module.
- `--binding <name>` — same but resolved via `--modules`.
- `--min-size <n>` / `--max-size <n>` — size predicates.
- `--cycles-only` — `size >= 2 && is_cycle`.
- `--singletons-only` — `size == 1`. Mutually exclusive with
  `--cycles-only`.
- `--residual-only` — every member module is under a `residual/`
  path (i.e. a clean extraction candidate not yet promoted).

```bash
# Every cycle in the current pipeline output, one per line.
debundle scc \
    --graph reports/tree/static/index-DI2GynTv/owner_graph.json \
    --cycles-only --ndjson

# Singleton SCCs that live inside residual — extraction candidates.
debundle scc \
    --graph reports/tree/static/index-DI2GynTv/owner_graph.json \
    --singletons-only --residual-only --ndjson \
    | jq -c '{id, modules}'
```

### `debundle cluster --module <path> | --binding <name>`

List the 1-hop neighbors of a module in the quotient graph. Each
record carries `direction` (`inbound` / `outbound` / `both`),
`edge_count`, and a `constrains_init_order` bit.

```bash
debundle cluster \
    --graph reports/tree/static/index-DI2GynTv/owner_graph.json \
    --modules tana/re/web/78d928dca7/spec/modules \
    --binding XOe --ndjson
```

### `debundle peel ...`

Pre-existing planning surface — `peel plan-work`, `peel units`,
`peel patch-plan`, `peel explain`, `peel source-slice`,
`peel graph-summary`. See `peel/plan.rs` for the documented shape.
The orthogonal commands above can subsume most everyday use; reach
for `peel` when you want factorizer-specific proposals /
diagnostics, not when you just want to look up one binding.

## Recipes

### "Which SCC contains this binding?"

```bash
debundle scc \
    --graph $GRAPH --modules $MODULES \
    --binding XOe
```

### "What size-1 SCCs are inside residual?"

```bash
debundle scc --graph $GRAPH \
    --singletons-only --residual-only --ndjson \
    | jq -c '{id, modules}'
```

### "Show me the source for this binding"

```bash
debundle binding show-code \
    --graph $GRAPH --modules $MODULES \
    --source-root $SNAPSHOT \
    XOe | jq -r '.slices[0].text'
```

### "Move a binding into a module, then regen"

```bash
debundle binding assign \
    --modules $MODULES \
    XOe runtime/plugins
bazelisk run //tana/re/web/<version>:regen_js
```

### "Try a hypothetical spec edit and see what changes"

This is a shell recipe, not a CLI flag. Save the current spec, edit,
rebuild, read the cycle / SCC report, revert if bad.

```bash
cp -r $MODULES /tmp/modules.before
debundle binding assign --modules $MODULES \
    XOe runtime/plugins
bazelisk build //tana/re/web/<version>:debundle --config=rbe \
    --remote_upload_local_results=false
# Inspect new cycle / SCC shape:
debundle scc --graph $GRAPH --cycles-only --ndjson | wc -l
# Revert if needed:
rm -rf $MODULES && mv /tmp/modules.before $MODULES
```

### "Rename a module"

Plain `mv`. The spec compiler infers module path from file
location. Update any explicit cross-module references afterwards.

```bash
mv $MODULES/runtime/plugins.yaml $MODULES/runtime/plugin_settings.yaml
```

### "Disable a YAML"

Plain `mv`. The spec compiler ignores files that don't end in
`.yaml`.

```bash
mv $MODULES/runtime/plugins.yaml $MODULES/runtime/plugins.yaml.disabled
```
