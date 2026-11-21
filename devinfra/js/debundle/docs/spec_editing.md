# Editing the spec: modules and bindings

Inspecting, proposing, moving, merging, and renaming modules and bindings;
`comment:` fields. Operational base: `cli.md`.

## References to logical members

`chunk_analysis_options.<chunk>.initializer_call_effect_inputs[].resolved_member`
names an existing logical member by `{module, name}`. Its key follows the
logical module path and readable member name, so an owner rename changes the
reference. `bindings rename`, `bindings assign`, `bindings unassign`,
`modules merge`, `modules apply`, and `modules delete --force` edit the
logical-module tree but do not read or rewrite `chunk_analysis_options`. When
one of these edits changes or removes a referenced module path or member name,
update or remove every referring input in the same authoring change. The normal
source-analysis resolver rejects missing or stale references; it never falls
back to a source selector or raw binding spelling. References to
`chunk_renames` entries are unsupported because those entries do not create
logical-module ownership claims.

## Workflow: executing a delegated assignment

Use the [snapshot-backed adapter contract](../skills/shared/workflow.md#adapter-contract)
for required inputs and [the task chooser](../README.md#choose-a-task) for the
operation-specific recipe.

1. Confirm the worker checkout's expected base and verify its starting snapshot.
   Use the copied engine, roots, and package map from the adapter contract for all
   graph-backed commands. Keep the baseline verification's actual result.
2. Check the assignment against source, callers, dependencies, and atomic coverage.
   Apply project conventions and [module-shape guidance](../skills/shared/module_shape.md). Expand
   within the assigned scope when needed to keep groups whole; skip or return
   work whose destination or required scope remains unresolved.
3. Follow the task chooser's recipe for the selected operation. It owns the
   command's chunk/YAML scope, batch format, dry-run review, and gate semantics.
   For a fresh source-unit assignment, inspect the source and callers before
   choosing destinations. The [selector guide](selectors.md) owns portability
   criteria; a current-source proof does not establish future stability.
4. Apply the reviewed edit and retain its proposed-assignment gate result. Remove
   now-owned entries from the project's non-emitting rename/annotation patch
   stream when applicable. Review attached comments/notes against current source
   and gate evidence: stale ownership explanations can survive a correct move.
   Transfer meaningful module comments before deleting empty shells; preserve
   actual author contracts and selector-debt provenance. Never edit the upstream
   bundle or generated output.
5. Regenerate through the worker's adapter command, then run its uniqueness,
   runtime, and applicable CI checks. Record the results separately using
   [execution evidence](../skills/shared/workflow.md#execution-evidence).
6. Export the exact relative YAML additions, modifications, and deletions from the
   snapshot into one reviewable branch. The commit transports the patch; the
   snapshot run and its execution records prove what was validated.

Graph-backed binding moves, merges, deletes and source-unit batches use the same
complete placement validator, including when repairing a rejected baseline.
Unchanged claims retain their effective owners, including generated residual
modules. Unclaiming under `inline_in_entry` routes the removed claim to entry.
For other `unassigned_mode` policies, graph-only unassign/delete cannot determine
the materializer's new residual partition and refuses before writing. Validate
those candidate YAML changes through canonical materialization (`debundle run`);
`--no-verify` only skips the edit gate and does not establish validity. The policy
comes from fingerprint-verified authoring configuration, not from whether the
baseline happens to contain residual modules.

A successful edit makes the saved graph describe the pre-edit YAML. Regenerate
before any subsequent graph-backed query or edit. Starting `snapshot verify`
is expected to fail after edits; it is not a post-edit gate.

For broad residual discovery, run `modules propose` without seed units or a
preselected owner and inspect its complete accepted assignment. If the
destination is architecturally unclear, return it to the architect. Use
[failure routing](../skills/shared/workflow.md#failure-routing) for graph and
gate failures.

## Inspect ownership before editing

Start with a bounded `describe <module-path> --limit 20`, or pass known owner
paths to `describe --batch owners.json --limit 20`. Then inspect specific
bindings with `--direction incoming` and `--reason lazy_use` when looking for
lazy consumers. These are filtered graph views, not a complete ownership or gate
proof. Check the reported omitted counts and read the source behind candidate
consumers; use `--all` for an intentional exhaustive artifact rather than as the
first query over a high-fanout family.

## Workflow: reviewing existing module boundaries

Use `describe`, `coverage`, `atoms` and source inspection to understand current
claims and structural constraints. Choose semantic destinations from source
behavior and callers. Check a complete `{unit_id, module}` map with
`modules check --batch changes.json`. Preview with
`modules apply --assignments changes.json --dry-run`, then apply the same
assignments without `--dry-run`. Exact apply preserves existing selector entries
and their metadata, including grouped source matches and anonymous statements.
The [CLI guide](cli.md#applying-explicit-assignments-modules-apply---assignments)
defines the batch and validation contract.

## Workflow: proposing residual modules

Use [residual proposals](module_proposals.md) to derive groups under the
append-one-owner, baseline-preserving entry-residual policy:

1. **Inspect.** Review `graph-summary`, residual `atoms`, relevant source and
   edge evidence with `describe` and `inspect-source --id`.
2. **Propose.** Run `debundle modules propose --format json` without a seed. The
   constructor derives feasibility for all entry-residual atoms; inspect the
   outcome before saving its executable `.plan`.
3. **Review.** Read every source-unit destination in the plan and check the
   semantic boundaries, fresh paths, required companions and final import
   priority. Changed or combined decisions need a complete check.
4. **Check and preview.** Run `modules check --plan plan.json`, then
   `modules apply --plan plan.json --dry-run --format json`. Review the
   synthesized selectors and the resolved final assignment.
5. **Apply and run.** Apply the same plan, materialize the result, run relevant
   behavior checks and refresh the graph before another graph-backed edit.

`found` includes graph acceptance and selector-writer preflight. Materialization
and runtime behavior remain separate evidence. `exhausted_no_peel` applies only to the
reported scope; `incomplete` and errors do not establish that no placement
exists.

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

`sym` and `module` are required; `readable` is optional. `--batch` accepts only move objects in the JSON shape shown here.
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
2. **Inspect the atom.** Run `debundle describe <atom-id> --all` for graph
   context (which source units are in it, why they're co-bound), then
   `debundle inspect-source --id <atom-id>` to read the source.
3. **List atoms broadly** if you need to triangulate:
   ```bash
   debundle atoms --format json | jq '.[] | select(...)'
   ```
   `debundle coverage` reports per-atom spec coverage; rows tagged
   "split" by current YAML are not landable until the assignment agrees.
4. **Diagnose before choosing the repair.** Follow the shared
   [assignment diagnosis](../skills/shared/workflow.md#diagnose-a-rejected-assignment)
   to distinguish a genuinely unsafe split from conservative analysis or a tool
   defect. If the complete atom forms a coherent module, include its missing
   members in the move batch; the diagnostic does not compute that completion.
   Otherwise revise the boundary or investigate the blocking proof.
5. **Re-run** the normal gate on the candidate; co-location alone does not
   establish acceptance.

The gate refuses; nothing on disk has changed.

## Workflow: proposing commutations

When `gate cut` shows a blocking SCC whose cut is `sequenced` edges with
opaque effects (an unknown call, an escaped global alias), co-location is not
the only remedy: a `commuting_statements` entry
([SPEC § Commuting statements](../SPEC.md#commuting-statements)) certifies that
the two source units are order-independent during initialization.

1. **Draft.** Run `debundle spec propose-commutations --graph <graph>
--source-root <root> [--modules <chunk tree>] --out commutations.yaml`
   ([cli.md § Proposing commutations](cli.md#proposing-commutations-spec-propose-commutations)).
   It proposes one entry per `sequenced` source-unit pair inside the SCC that
   the gate would accept, the best 50 by default (`--all` for every one),
   ranked single-pair cuts first (the pair that alone
   breaks the SCC may sit on an arc the gate's cut did not pick), and reports
   separately the pairs the gate would
   reject, the endpoints without a unique selector, and the `eager_use` edges
   that still need co-location. Its `sccs` rows say whether all proposals
   together break each SCC; when they cannot, the residual component member
   paths and blocking edge counts show a surviving constraint. Predictions
   preserve third-statement order through the same closure as the gate.
   `counts_are_lower_bounds` marks an analysis stopped after proving a cycle;
   its reported cyclic groups and counts need not be the full inventory.
2. **Audit.** For each proposal read both statements at the reported locations
   and establish the premise yourself: neither observes the other's effect.
   The explanation says only why the analysis could not prove the order
   irrelevant; the draft is a candidate, not a fact.
3. **Apply.** Copy the audited entries into
   `chunk_analysis_options.<chunk>.commuting_statements` of the spec (the
   draft is already grouped under that key) and write the `note` with the
   reason the pair commutes. Drop the rest.
4. **Validate.** Run `debundle run` (or `spec validate`): the gate rejects an
   entry that contradicts a source fact and warns about one that inerts
   nothing, and the rejection report shows what the accepted entries bought
   (`inert_commuting_edges`).

## Workflow: applying a reviewed source-unit assignment

For exact source-unit movement, use `modules apply --assignments`. The batch is
an array of `{ "unit_id": "...", "module": "..." }` changes. Use current
source-unit IDs and extensionless logical module IDs. Omitted units retain
their current destinations. Exact apply can move existing claims, claim
residual units, and move anonymous statements in the same complete candidate.
It preserves selectors and metadata on existing claims, including grouped
`source_matches` and annotations. Reviewed `--selector-overrides` can provide a
source-backed selector for a newly claimed binding when synthesis is not the
right anchor. Preview with `--dry-run`; preview and write share the same
selector-resolution and complete assignment validation. See the [CLI guide](cli.md#applying-explicit-assignments-modules-apply---assignments).

Both `modules apply` and `modules merge` consolidate compatible source-match
records in their destinations when the template bytes, identifier policy and
selector note match and local capture names do not overlap. Captures retain
order and export aliases; binding annotations and module metadata are preserved.
Different notes or incompatible capture aliases remain separate records.
Consolidation happens before normal source resolution and assignment checks.

## Peel heuristics: patterns that should move together

These patterns surface repeatedly when extracting minified bundles. Atomic
groups and exact graph placement do not determine semantic ownership; inspect
the source and callers before choosing a destination.

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
`anonymous_statements:` — the chunker just happened to place it separately.
The source and graph support this reviewed ownership decision.

### Adjacent-ordinal singletons referencing each other

If source units at consecutive statement ordinals `N` and `N+1` are each
the other's only edge target/source, they were lines next to each other in the
source and the residual policy placed them separately. Review them as one
candidate assignment.
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

A graph-checked merge that creates a target owner must receive the complete
final nonentry `import_order` as a JSON array via `--import-order`. The gate
checks that exact order and stores it in the verified tree config with the
module edit; it does not infer a schedule. Keep the owner list and order
together when reviewing or integrating the merge.

The edit gate checks direct claims against the dependency graph and, for typed
`kind` pins, the original source before checking the post-merge assignment.
Pass `--graph <dependency_graph.json.gz>` for the chunk's edge topology and
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
optional `comment:` and `note:` strings; the config's `host_behavior` block
accepts `note:` only, for the audited executable's identity (<../SPEC.md> § Host
behavior). `comment:` survives rebuilds and emits
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
`initializer_purity`, `initializer_binding_effects`, `pure_members`, and
`no_sync_callback_members`.

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
