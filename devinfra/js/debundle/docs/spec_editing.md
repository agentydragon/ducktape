# Editing the spec: modules and bindings

Proposing, moving, merging, and renaming modules and bindings; peel heuristics;
`comment:` fields. Operational base: `cli.md`.

## Workflow: proposing new modules

Use the factorizer to surface what's currently extractable:

1. **`debundle modules propose --format json > proposals.json`** —
   factorizer proposals + diagnostics derived from the atomic DAG.
2. **Skim the diagnostics.** Each diagnostic carries a `reason`
   explaining why a closed atomic-DAG set could not become a proposal
   (currently `exceeds_size_cap`: the spec edit is larger than
   `--size-cap-lines`).
3. **Apply reviewed binding-only proposals** (see the move workflow
   below). `bindings assign --batch` accepts selected proposal objects
   when every selected row maps to member moves: `landable_today: true`,
   non-empty `binding_ids`, no `merge_into`, and no
   `anonymous_statement_owner_ids`.

```bash
jq '[.proposals[]
     | select(.landable_today
       and (.merge_into | not)
       and (((.binding_ids // []) | length) > 0)
       and (((.anonymous_statement_owner_ids // []) | length) == 0))]' \
  proposals.json > selected-proposals.json
debundle bindings assign --batch selected-proposals.json --dry-run
```

`merge_into` rows are emitted-output proposal evidence, not direct
`bindings assign` moves; use `debundle modules merge --target ...` or
manual YAML after choosing the target. Rows with
`anonymous_statement_owner_ids` need `anonymous_statements:` edits.

Rows with `status: blocked_residual_dependency` are never
`landable_today`: the cell reads other residual cells
(`other_residual_cells_referenced`), so promoting it alone would trip
the realizability gate. Grow the closure — assign the proposal
together with the referenced cells in one batch — or co-locate the
owners manually before assigning.

For aggregate counts before drilling in:

```bash
debundle graph-summary --format text
```

Use `debundle graph-summary --include-proposals` only when aggregate
proposal and diagnostic counts matter; the default is a fast graph
summary.

For a focused look at one proposal before applying:

```bash
debundle describe <proposal-id>
debundle show-source <proposal-id> --context-lines 40
```

## Workflow: moving a binding from one module to another

`debundle bindings assign` is the move primitive. Each positional
argument is colon-separated: `<sym>:<module>[:<readable>]`.

### Single move, keep current name

```bash
debundle bindings assign XOe:runtime/plugins
```

`<sym>` accepts minified (`XOe`) or readable
(`PluginSettingsAccessor`) form. The destination module is
auto-created if it doesn't yet exist; the source module is auto-deleted
when its `members:` becomes empty **and** its top-level `comment:` is
empty/absent (modules with a comment are kept as `members: []`
shells).

### Move + rename in one step

```bash
debundle bindings assign XOe:runtime/plugins:PluginSettingsAccessor
```

The optional third field sets the new readable `name:`. Validation
includes name-collision detection.

### Batch move

Multi-binding refactors run as one atomic operation: validation is
checked on the post-batch spec, not after each individual move. This
lets the intermediate state be "invalid" (e.g. half-moved atom) so
long as the final state is valid.

```bash
debundle bindings assign \
    XOe:runtime/plugins:PluginSettingsAccessor \
    YOe:runtime/plugins \
    ZOe:runtime/widgets:WidgetRegistry
```

For large refactors, pipe JSON:

```bash
debundle bindings assign --batch moves.json
debundle bindings assign --batch - < moves.json
```

JSON shape:

```json
[
  { "sym": "XOe", "module": "runtime/plugins", "readable": "PluginSettingsAccessor" },
  { "sym": "YOe", "module": "runtime/plugins" },
  { "sym": "ZOe", "module": "runtime/widgets", "readable": "WidgetRegistry" }
]
```

`--batch` also accepts `modules propose --format json` output, or a
filtered proposal array, when every selected proposal maps cleanly to
member moves. It refuses non-landable rows, `merge_into` rows, rows
without `binding_ids`, and rows containing
`anonymous_statement_owner_ids`; use an explicit move array when you
need `readable` renames.

`sym` and `module` are required; `readable` is optional. Array order
controls dedupe (last-wins on duplicate `sym`).

### Default validation, `--dry-run`, `--no-verify`

```bash
# Default: validate the post-batch spec; refuse if invalid; apply if valid.
debundle bindings assign XOe:runtime/plugins

# Preview only: run validation, print verdict, don't modify any file.
debundle bindings assign --dry-run XOe:runtime/plugins

# Apply without validating. Escape hatch for intentional intermediate states.
debundle bindings assign --no-verify XOe:runtime/plugins
```

`--dry-run` + `--no-verify` together: show what would change without
validating. Useful for inspecting an intermediate that you know
violates the gate.

## Workflow: renaming a binding without moving

```bash
debundle bindings rename XOe PluginSettingsAccessor
```

`<original>` accepts minified or current readable form. Validation is
name-collision detection (no two bindings share the same readable name
within the chunk). Mostly a convenience over `bindings assign` for the
rename-only case. `--no-verify` / `--dry-run` available.

## Workflow: fixing an atom-split rejection

When `bindings assign` rejects with an "atom split" diagnostic, the
realizability gate found that the requested move would split an
indivisible owner set. The diagnostic names the split atom, its owners,
and the destinations each member would land in.

1. **Read the diagnostic.** It names the atom, lists each owner's
   current and proposed module, and the `DepKind` causes (eager_use,
   rebind, sequenced, ...).
2. **Inspect the atom.** Run `debundle describe <atom-id>` for graph
   context (which owners are in it, why they're co-bound), then
   `debundle show-source <atom-id>` to read the source.
3. **List atoms broadly** if you need to triangulate:
   ```bash
   debundle atoms --format json | jq '.[] | select(...)'
   ```
   `debundle coverage` reports per-atom spec coverage; rows tagged
   "split" by current YAML are not landable until the partition agrees.
4. **Either expand the move set or revisit the partition.** The
   diagnostic does not auto-compute the minimal completion ("also move
   `rRe` and `MRe`"); read the printed atom membership and add the
   missing moves to your batch.
5. **Re-run** with the expanded batch.

The gate refuses; nothing on disk has changed.

## Peel heuristics: patterns that should move together

These patterns surface repeatedly when peeling minified bundles. Each
one usually appears as a singleton or two-statement `auto_partition_*`
shell whose source happens to live one line below a named binding;
the factorizer can't always see the textual adjacency, so the human
peeler (or a custom propose-side heuristic) makes the call.

### TypeScript decorator wirings live with the class they decorate

TypeScript / Babel emits class-field decorators as standalone
top-level statements right after the class declaration. The shape
varies by toolchain but the body is always
`__decorate([…decorators], <Class>.prototype, "<member>"[, <kind>])`
where the wrapping function name is minified (`t0`, `Q0`, `b0t`,
`__decorate`, etc.). They surface as anonymous statements in the
bundled output. Two reliable signals:

1. The first positional argument is an array literal of decorator
   references (`[Z]`, `[oe]`, `[ee, ie]`, …) — the bundler doesn't
   inline that array for any other call shape, so this is a
   high-precision fingerprint.
2. The second argument is `<Class>.prototype` where `<Class>` is a
   binding the spec already owns.

When both hold, the statement belongs in `<Class>`'s module via
`anonymous_statements:` — the chunker just happened to split it into a
sibling shell. Absorbing them costs zero realizability headroom (the
edges they carry already point at the class).

### Adjacent-ordinal singletons referencing each other

If owners at consecutive statement ordinals `N` and `N+1` are each
the other's only edge target/source, they were lines next to each
other in the source and the partitioner accidentally put them in
sibling shells. Merge the two `auto_partition_*` modules into one.
Detectable via `cluster(A).out == [B] && cluster(B).in == [A]` plus
`|N+1 - N| == 1`.

### `system_ids` + one heuristic

Some modules are _ambient_ — every other module touches them but
they don't constrain placement. When a singleton has exactly two
neighbors and one is an ambient module (e.g. `domains/system/ids`
holding chunk-wide enum constants, or `infra/vite/asset_map`
holding the chunker's dynamic-import lookup), treat the singleton
as effectively single-neighbor and absorb it into the _other_
named home.

### Lazy-loaded React wrappers fold into their consumer

`const X = b.lazy(() => bt(() => import("./<chunk>-<hash>.js")))` is
a one-liner. Its only non-`asset_map` neighbor is the module that
mounts `<X />`. Move `X` into that consumer's YAML.

### Commenting `anonymous_statements:` entries

Each `anonymous_statements:` entry accepts an optional `comment:`
**or** `note:` field — both are `Option<String>`, both are
preserved on round-trip. The materializer resolves statements from
`match:` and emits `comment:` immediately before the matched statement
in generated JS; `note:` remains YAML metadata. Graph-backed CLI
checks resolve the same selector against source and map the matched
statement back to the owner graph, so the spec stays selector-based.
For what to put in `comment:` vs. `note:`, see <../README.md> →
"Comments".

```yaml
anonymous_statements:
  - source_match:
      match: "Foo.prototype.bar = true;"
    comment: |
      Enables Foo.bar before consumers import Foo.
  - source_match:
      match: "registerFoo(foo);"
    note: "uncertain: looks like a registration side effect"
```

`comment:` is accepted on `anonymous_statements:` entries even
though the field originated at module and per-member level — the
spec rejects unknown fields otherwise, and authors who reach for
the familiar spelling shouldn't hit a cryptic parse error.

## Workflow: merging two modules

```bash
debundle modules merge --target <T> <source1> [<source2> ...]
```

Splices `members:`, `source_matches:`, `annotations:`, and
`anonymous_statements:` from each source YAML into `<T>`; creates `<T>` if
needed, then deletes the source YAML files.
Module-file args may include or omit `.yaml`, so `runtime/plugins`
and `runtime/plugins.yaml` name the same module file.

```bash
# Preview only — print verdict and planned diff summary.
debundle modules merge --dry-run --target runtime/plugins runtime/widgets

# Apply.
debundle modules merge --target runtime/plugins runtime/widgets
```

Source-module `comment:` content is concatenated into the target's
module-level `comment:` with a `--- from <source>:` divider when
sources have non-empty comments.

The realizability gate runs against the post-merge partition before
the YAML splice fires — pass `--graph <owner_graph.json>` so the
gate has the chunk's edge topology. The gate rejects merges that
would create cross-module cycles; `--dry-run` runs the gate without
writing. Use `--no-verify` to skip the gate (e.g. during multi-step
refactors where an intermediate state is intentionally invalid).

## Workflow: authoring `comment:` fields

Module YAMLs and binding annotations carry the same optional `comment:` field as
`anonymous_statements:` entries (see above):

```yaml
# Module YAML
comment: |
  Coordinates foo registrations and lookup state.

members:
  - name: FooAccessor
    selector:
      binding: { name: a, kind: variable_declarator }

annotations:
  FooAccessor:
    comment: |
      Reads the active foo registry without mutating it.
```

Edit module and member comments via the CLI (assumes
`DEBUNDLE_MODULES` is exported; pass `--modules <dir>` otherwise):

```bash
# Set a member's comment from a positional arg.
debundle bindings comment a "Accessor for foo state."

# Open $EDITOR (fallback $VISUAL, then vi) pre-populated with the current comment.
debundle bindings comment a --edit

# Read the current comment (plain text on tty, JSON on pipe).
debundle bindings comment a

# Remove the comment entirely.
debundle bindings comment a --clear

# Same three modes for module-level comments.
debundle modules comment runtime/foo --edit
```

`<sym>` accepts minified or readable; `<module>` is the module path
relative to `$DEBUNDLE_MODULES`.

Move semantics (CLI surface, not a separate feature):

- `bindings assign` carries a binding's `annotations.<export_name>` entry with
  the binding as it moves between modules.
- `bindings assign` auto-deletes a drained source module only when its
  module-level `comment:` is empty/absent.
- `modules merge` concatenates source-module comments into the target's
  module-level `comment:` (with a `--- from <source>:` divider) when
  sources have non-empty comments, and records `merged from: <sources>`
  provenance in the target's module-level `note:` (see <../README.md> →
  "Comments").

CLI editing is live for module and member comments; anonymous
statement comments are authored directly in YAML.

When a binding cannot yet be stabilized because the selector language lacks a
concise matcher, leave `annotations.<export_name>.note` recording the concrete
matcher/tooling blocker and the desired future feature instead of silently
keeping minified binding debt. Use `note:`, **not** `comment:`: `note:` is inert
(YAML-only, never emitted to generated JS), so it annotates the debt without
changing byte-identical output, and the keep-going selector report surfaces
noted name-pins as `annotated_debt` for a repair flow to route:

```yaml
annotations:
  ExportName:
    note: |
      blocked on Ducktape support for <specific matcher/tooling capability needed here>
```

For grouped selectors, put per-export debt under
`annotations.<export_name>.note`. `comment` emits into generated JS; `note` does
not.

```yaml
source_matches:
  - match: "const x = EXPR_X, y = EXPR_Y;"
    bindings:
      - local: x
        name: exportedX
      - local: y
        name: exportedY
annotations:
  exportedX:
    note: "TODO: minimize selector once this helper has a narrower anchor."
```

Do not leave blocker notes for selector patterns Ducktape now supports, such
as matching one declarator inside a multi-declarator declaration or bracketing
object literal properties with `ANYTHING`.

## Renaming or disabling a module

Not a CLI operation — plain `mv` on the YAML file:

```bash
# Rename: the module path is re-derived from the new filename.
mv $DEBUNDLE_MODULES/runtime/plugins.yaml $DEBUNDLE_MODULES/runtime/plugin_settings.yaml

# Disable: any non-.yaml suffix makes the spec compiler skip the file.
mv $DEBUNDLE_MODULES/runtime/plugins.yaml $DEBUNDLE_MODULES/runtime/plugins.yaml.disabled
```

The next mutating command (or `debundle run`) re-validates and
surfaces any resulting atom split as a gate diagnostic.
