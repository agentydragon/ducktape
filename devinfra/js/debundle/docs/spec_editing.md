# Editing the spec: modules and bindings

Proposing, moving, merging, and renaming modules and bindings; peel heuristics;
`comment:` fields. Operational base: `cli.md`.

## Workflow: reorganizing existing modules

Use `modules decompose --atomic` to plan a complete assignment of existing
source units. Review semantic destinations, selector anchors, and the full
candidate verdict; optional coarsening and original-home rejoining are
described in [the decomposition guide](cli.md#refining-existing-modules-modules-decompose).

Apply a reviewed `{unit_id, module}` map of already-claimed non-residual
source units with `modules refine --dry-run`, then `modules refine`. Both resolve
the full scoped spec and validate the requested assignment before any write.
The [source-unit assignment guide](cli.md#applying-reviewed-source-unit-assignments-modules-refine)
defines batch fields, multi-chunk paths, and excluded source-unit kinds. Residual
extraction uses the proposal workflow below; focused binding moves use
`bindings assign`.

## Workflow: proposing new modules

Use the proposer to surface what's currently extractable:

For an app-level multi-chunk spec, pass `--tree-config spec_config.yaml`
to both `modules propose` and `modules apply` so proposals and the edit gate
use the module roots mapped to the graph's chunk.

1. **`debundle modules propose --format json > proposals.json`** —
   proposer proposals + diagnostics derived from the atomic DAG.
2. **Skim the diagnostics.** Each diagnostic carries a `reason`
   explaining why a closed atomic-DAG set could not become a proposal
   (currently `exceeds_size_cap`: the spec edit is larger than
   `--size-cap-lines`).
3. **Review fresh residual claims** with `modules apply --dry-run --format json`
   after choosing semantic destinations and readable names. Inspect every
   `selector_reviews` entry: check that its selector targets the intended binding
   and that retained literals and property names are reasonable rebuild anchors.
   A successful uniqueness proof is specific to the current source snapshot.
   See <cli.md> § Claiming fresh proposals and <selectors.md> for selector
   stability guidance.
4. **Apply the reviewed batch.** Supply a `selectors` override for a binding
   whose synthesized selector needs a better structural anchor, or omit an
   unready proposal from the batch so it stays residual. Then apply and run
   the project's extraction gate.

```bash
# Author reviewed.json with proposal_id, source_unit_ids, module, and readable_names.
debundle modules apply --batch reviewed.json --dry-run --format json
debundle modules apply --batch reviewed.json
```

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
debundle inspect-source --id <proposal-id> --context-statements 1
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
when its `members:` becomes empty and it has no module-level `comment:`,
remaining `source_matches:`, `annotations:`, or `anonymous_statements:`.
An explicit empty-string comment also keeps the module. Unrelated empty modules
are not swept.

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

`sym` and `module` are required; `readable` is optional. `--batch` also
accepts selected `modules propose` rows: <cli.md> § `--batch` JSON format.
Duplicate and contradictory entries: <cli.md> § Batch atomicity.

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

A rename can change other templates: a free identifier naming the old export
name stops being a reference and alpha-renames, and one naming the new name
becomes a reference, or makes its template `invalid` when several modules export
it (<../SPEC.md> § Matching). Rerun source-only `spec validate` afterwards and
check its outcomes and `templates` listing.

## Workflow: fixing an atom-split rejection

When `bindings assign` rejects with an "atom split" diagnostic, the
realizability gate found that the requested move would split an
indivisible source-unit set. The diagnostic names the split atom, its source units,
and the destinations each member would land in.

1. **Read the diagnostic.** It names the atom, lists each source unit's
   current and proposed module, and the `DepKind` causes (eager_use,
   rebind, sequenced, ...).
2. **Inspect the atom.** Run `debundle describe <atom-id>` for graph
   context (which source units are in it, why they're co-bound), then
   `debundle inspect-source --id <atom-id>` to read the source.
3. **List atoms broadly** if you need to triangulate:
   ```bash
   debundle atoms --format json | jq '.[] | select(...)'
   ```
   `debundle coverage` reports per-atom spec coverage; rows tagged
   "split" by current YAML are not landable until the assignment agrees.
4. **Either expand the move set or revisit the assignment.** The
   diagnostic does not auto-compute the minimal completion ("also move
   `rRe` and `MRe`"); read the printed atom membership and add the
   missing moves to your batch.
5. **Re-run** with the expanded batch.

The gate refuses; nothing on disk has changed.

## Workflow: applying a reviewed source-unit assignment

When `modules decompose` reports a useful source-unit assignment, replace its
temporary part labels with semantic module names and apply the complete
assignment through `modules refine`. For specs using local
`intrinsic_alias.referenced_by` anchors, pass
`--preserve-selector-anchors --source-root <source-root>` when decomposing so
the candidate keeps each alias with its resolved local anchor. For each source unit
that changes destination, include one row in a JSON batch:

```json
[
  { "unit_id": "unit:12", "module": "runtime/state" },
  { "unit_id": "unit:19", "module": "runtime/effects" }
]
```

Then preview and apply the same reviewed file:

```bash
debundle modules refine --batch reviewed-source-units.json --dry-run \
  --graph dependency_graph.json --modules spec/modules --source-root source
debundle modules refine --batch reviewed-source-units.json \
  --graph dependency_graph.json --modules spec/modules --source-root source
```

This command moves existing selector entries for each source unit as a unit. It
preserves their authored selectors and metadata, including all names in a
multi-binding source match. Source units not listed retain their current module.
For a multi-chunk tree, pass `--tree-config` and use destination paths prefixed
by that chunk's module root. The command resolves the full post-edit selector
set again, checks that every source unit still resolves to the reviewed destination,
then runs the shared atomic-group and realizability gates. It refuses residual,
unclaimed, import, and export source units and does not synthesize selectors. Module
comments and notes remain with their original files. See <cli.md> § Applying
reviewed source-unit assignments (`modules refine`).

Both `modules refine` and `modules merge` consolidate source-match records in
their destinations when the template bytes, identifier policy and selector note
are identical and the local capture names do not overlap. Captures retain their
order and export aliases; binding annotations and module metadata are preserved.
Different notes or incompatible capture aliases remain separate records.
Consolidation happens before the normal source-resolution and assignment gates.
An unchanged-owner `modules refine` batch can explicitly normalize an existing
destination: preview and apply still run the complete identity check and gate,
and report `dry-run` / `applied` when only selector grouping changes.

## Peel heuristics: patterns that should move together

These patterns surface repeatedly when peeling minified bundles. Each
one usually appears as a singleton or two-statement `auto_partition_*`
shell whose source happens to live one line below a named binding;
the proposer can't always see the textual adjacency, so the human
peeler (or a custom propose-side heuristic) makes the call.

### TypeScript decorator wirings live with the class they decorate

TypeScript / Babel emits class-field decorators as standalone
top-level statements right after the class declaration. The shape
varies by toolchain but the body is always
`__decorate([…decorators], <Class>.prototype, "<member>"[, <kind>])`
where the wrapping function name is minified (`t0`, `Q0`, `b0t`,
`__decorate`, etc.). They surface as anonymous statements in the
bundled output. Reliable signals:

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

If source units at consecutive statement ordinals `N` and `N+1` are each
the other's only edge target/source, they were lines next to each
other in the source and the decomposer assigned them to
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

Each `anonymous_statements:` entry can carry the `comment:` and `note:` fields
described below. The materializer resolves `source_match` and emits the comment
above the matched statement. Graph-backed CLI checks resolve the same selector
against source and map the matched statement back to the dependency graph, so the
spec stays selector-based.

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

Source-module comments and notes are composed as described in
§ Comments and notes below.

The edit gate checks direct claims against the dependency graph and, for typed
`kind` pins, the original source before checking the post-merge assignment.
Pass `--graph <dependency_graph.json>` for the chunk's edge topology and
`--source-root <dir>` when the graph's relative source paths are not otherwise
resolvable. It rejects missing or ambiguous local claims, mismatched typed
claims, and merges that would create cross-module cycles or split an atomic
group. `--dry-run` runs these checks without writing. Use `--no-verify` to
skip validation (e.g. during multi-step refactors where an intermediate state
is intentionally invalid).

A successful edit gate covers source claims, the post-merge assignment, and
realizability checks. It does not establish complete reconstruction
acceptance; `debundle run` remains the end-to-end acceptance path for vendor
boundaries, exports, reconstruction, and browser planning.
The source-claim check recognizes named, default, and namespace imports as
import specifiers, while module emission currently supports moving named
imports only (`lowering/imports/imports_runtime.rs`).

## Comments and notes

Module YAMLs, binding annotations, and `anonymous_statements:` entries accept
optional `comment:` and `note:` strings. `comment:` survives rebuilds and emits
into generated JS: a module comment at the top of its file, a binding comment
above its declaring statement, and an anonymous-statement comment above its matched
statement. An empty comment emits nothing. Claimed declarators of one
`var`/`let`/`const` list emit as separate statements, each with its own comment;
bindings of one destructuring declarator share a statement, with their comments
stacked in pattern order.

Use `comment:` to explain intent, invariants, and module relationships. Keep
debt rationale, provenance, source-unit IDs, and source-call trivia in non-emitting
`note:` (or omit them). Both fields survive YAML rewrites; `#` YAML comments do
not, because the rewriters re-emit the files without them. Per-binding metadata
belongs under `annotations.<export_name>`, which also accepts `purity`, `effect`,
`initializer_purity`, `pure_members`, and `no_sync_callback_members`.

On `modules merge`, non-empty source-module comments join the target comment
with `--- from <source>:` dividers. The command adds `merged from: <sources>`
provenance to the target's module-level `note:`, composing with any existing
note. `bindings assign` carries a binding's annotations when moving it and
keeps a drained source module if its module-level `comment:` is present,
including an explicit empty string.

### Workflow: authoring `comment:` fields

Module YAMLs and binding annotations use the same fields as
`anonymous_statements:` entries:

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

Edit module and binding comments via the CLI. Binding comments are stored in
`annotations.<readable-or-minified-name>.comment`, including source-match
bindings (assumes
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

# The same modes for module-level comments.
debundle modules comment runtime/foo --edit
```

`<sym>` accepts minified or readable; `<module>` is the module path
relative to `$DEBUNDLE_MODULES`.

Anonymous-statement comments have no CLI; author them in YAML.

A binding that cannot yet be stabilized records its blocker in a `note:`:
<selectors.md> § Selector debt.

## Renaming or disabling a module

Rename whole modules with the gated `modules merge --batch` workflow in
<cli.md#whole-module-merge-batches>, using one source and a new target per entry.
This retains attached claims and metadata and validates the whole batch before
writing. Explicit `.yaml` targets preserve filename casing.

To disable a module, a non-`.yaml` suffix makes the compiler skip the file:

```bash
# Disable: any non-.yaml suffix makes the spec compiler skip the file.
mv $DEBUNDLE_MODULES/runtime/plugins.yaml $DEBUNDLE_MODULES/runtime/plugins.yaml.disabled
```

The next mutating command (or `debundle run`) re-validates and
surfaces any resulting atom split as a gate diagnostic.

## YAML text preservation

Edit commands may reserialize a whole YAML document, change unrelated formatting
and drop comments. Preserving untouched text is not an authoring requirement.
Semantic correctness, validation before writes, atomic replacement and accurate
dry-run reporting remain requirements. Persist important annotations in supported
spec fields such as `note:`, not comments that a serializer may discard.
