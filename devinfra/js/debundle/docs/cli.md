# debundle CLI

Cross-command semantics of the `debundle` CLI: conventions, pipelines, and
interactions that span commands. The per-command and per-flag reference is
the clap doc-comments themselves — `debundle <command> --help` — and the
code wins wherever prose and binary disagree. Future CLI ideas live in
`TODO.md`.

The source-built CLI target is `@ducktape//devinfra/js/debundle:debundle` in
consuming repositories, or `//devinfra/js/debundle:debundle` inside Ducktape.
Prefer the project's CLI wrapper when provided: it supplies the matching
graph, modules tree, and source root. All commands share the same
JSON-on-stdout / structured-diagnostic-on-stderr convention.

## Find a workflow

- Transform a spec: [Running the pipeline](#running-the-pipeline).
- Inspect a binding or source: [Workflow: investigating a binding
  end-to-end](#workflow-investigating-a-binding-end-to-end) and
  [Inspecting source](#inspecting-source).
- Edit assignments or claim proposals: [Batch atomicity](#batch-atomicity-bindings-assign),
  [Claiming fresh proposals](#claiming-fresh-proposals-modules-apply), and
  <spec_editing.md>.
- Reorganize existing modules: [Plan a module assignment](#refining-existing-modules-modules-decompose),
  then [apply reviewed source-unit assignments](#applying-reviewed-source-unit-assignments-modules-refine).
- Diagnose a rejected edit: [Rejection diagnostics](#rejection-diagnostics)
  and [Gate queries](#gate-queries).

Use `debundle <command> --help` for the current command and flag inventory.

## Common arguments and env vars

Most commands take these paths, each as a flag or an env var; the flag wins if
both are set.

| Flag                            | Env var                      | Meaning                                                                                                                                                          |
| ------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--graph <path>`                | `DEBUNDLE_GRAPH`             | `dependency_graph.json` for the chunk being inspected or edited. The graph path implies the chunk; multi-chunk callers point at different graphs per invocation. |
| `--modules <dir>`               | `DEBUNDLE_MODULES`           | Per-module YAML tree root (the directory under `spec/modules/`).                                                                                                 |
| `--source-root <dir>`           | `DEBUNDLE_SOURCE_ROOT`       | Upstream snapshot root containing the original chunk bytes, used to resolve source-backed selectors and typed binding pins.                                      |
| `--tree-config <file>`          | `DEBUNDLE_TREE_CONFIG`       | For graph-backed `modules` edits and queries, scope an app-level modules tree to the chunk named by `--graph`.                                                   |
| `--spec-root <dir>`             | `DEBUNDLE_SPEC_ROOT`         | Current common root of the tree config, vendor marks and modules after relocation. Global flag; the flag wins over the env var.                                  |
| `--graph-source-root <dir>`     | `DEBUNDLE_GRAPH_SOURCE_ROOT` | Current root of the analyzed input files after relocation. Distinct from `--source-root`, which resolves selector source paths. Global flag; the flag wins.      |
| `--graph-package-root NAME=DIR` | —                            | Current root for a package input in the saved graph after relocation. Repeat for each relocated package. Global flag.                                            |

Graph-backed spec queries and edits compare the saved graph's engine and
captured input bytes and YAML path membership with the current roots before
using the graph. `--modules` must name the graph chunk's exact configured
subtree. Commands accepting `--tree-config` may name the full modules tree
when that flag names the config captured by the graph. A changed source,
YAML, config, dependency assumption, wrong chunk tree, or old graph without
provenance exits nonzero. JSON and NDJSON commands emit a typed
`graph_compatibility` rejection with `mismatch.reason`, expected/actual
values, and `regenerate_command` argv. A successful edit leaves the saved
graph describing the pre-edit spec; rerun `debundle run` before another
graph-backed edit. Flat-spec graphs have no authored module-tree scope and
cannot authorize a YAML-tree edit or query.

`debundle run --out-root` reads `DEBUNDLE_OUT`. `debundle run
--tree-source-root` (the spec-tree compile root for source-relative paths in
the tree-shaped config) reads its own env var, `DEBUNDLE_TREE_SOURCE_ROOT` —
deliberately **not** `DEBUNDLE_SOURCE_ROOT`, because the two roots are
different directories in real corpora (spec tree vs. upstream snapshot).

Export the env vars once per shell session and subsequent commands run
without repeating the flags:

```bash
export DEBUNDLE_GRAPH=<debundle-output>/reports/tree/<chunk-id>/dependency_graph.json
export DEBUNDLE_MODULES=<spec-root>/<version>/modules
export DEBUNDLE_SOURCE_ROOT=<debundle-output>/app
export DEBUNDLE_OUT=<debundle-output-root>

debundle describe XOe
debundle scc --binding XOe
debundle bindings assign XOe:runtime/plugins
```

If remote execution downloads only minimal outputs, request full outputs so
the report side files are local: `--remote_download_outputs=all`.

## Output format

Read-only commands accept `--format <text|json|ndjson>`:

- `text` — human-readable default for interactive use.
- `json` — single JSON document, parseable with `jq`.
- `ndjson` — one JSON value per line, for streaming consumers (`jq -c`,
  piping to other commands). Reach for it on many-row streaming queries
  (`debundle scc --format ndjson` over a large graph).

If `--format` isn't passed and stdout is **not** a tty (i.e. the command is
in a pipeline), the default flips to `json`. So
`debundle modules propose | jq …` works without an explicit `--format json`.

Read-only inspection commands prefer fast graph/spec lookups. `modules
propose` is the command that runs the proposal proposer by default;
`describe`, `coverage`, and `graph-summary` do not run it unless the
selection itself is a proposal/diagnostic id or `--include-proposals` is
passed (it is expensive on large graphs), and proposal-derived JSON fields
are omitted when the proposer is skipped.

Description proposal fields and residual dependency names are defined in
<../SPEC.md#authoring-reports>; truncation metadata uses the same section names.

The mutating verbs (`bindings assign`, `bindings unassign`,
`bindings rename`, `modules merge`, `modules delete`) take the same
`--format` flag with the same tty/pipe default. Under `text` they print a
one-line verdict (`<action>` plus move/file counts). Under a JSON format
each verb prints **one outcome object** sharing a common schema core:

| Field           | Values                                                                                                                             |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `verb`          | `assign` \| `unassign` \| `rename` \| `merge` \| `delete`                                                                          |
| `action`        | `applied` \| `dry-run` \| `noop` \| `unchanged` \| `rejected`                                                                      |
| `gate`          | `passed` \| `names_only` \| `skipped` (`--no-verify`) \| `not_required` (edit cannot change the assignment); success outcomes only |
| `files_written` | Files written (or, under `--dry-run`, that would be written)                                                                       |
| `files_deleted` | Files deleted (or, under `--dry-run`, that would be deleted)                                                                       |

Verb-specific fields flatten in alongside the core: `moves_applied`
(assign), `unassigned` (unassign), `binding` / `old_readable` /
`new_readable` (rename), `target` (merge). Gate rejections replace the
success object with a structured rejection object — see "Rejection
diagnostics" below.

## Name and identifier resolution

Every command that takes a binding name (`<sym>`) accepts **either form**
wherever the lookup is unambiguous:

- The _minified_ name from the chunk (e.g. `XOe`) — the stable
  hygiene-aware identity.
- The _readable_ name from the spec's `name:` field
  (e.g. `PluginSettingsAccessor`).

If both forms could match different bindings, the command refuses with a
list. Use the minified form to disambiguate.

`describe` and `inspect-source --id` take any ID kind and dispatch on shape:
bindings (minified or readable), module paths (`runtime/plugins`) or
unambiguous module filenames, module ids (`logical:7`), source-unit IDs
(`unit:42`), atom ids (`atomic:N`), proposal ids, diagnostic ids.

## Validate-by-default (mutating commands)

Every command that modifies the spec runs validation **by default** before
writing changes back to disk. For commands that affect the chunk's module
assignment (anything that moves a binding between modules), that means
the post-edit claim checks followed by the full realizability + atom-split
gate — which requires `--graph <dependency_graph.json>` unless `--no-verify` is set.
Direct local claims must identify graph source units, typed `kind` pins must match
one source chunk (a repeated declaration may satisfy the kind at any site),
and `import_specifier` pins must name an import specifier. Pass `--source-root` when
the graph's relative source paths are not otherwise resolvable. Renames run
name-collision detection; comment edits run shape preservation only (they take
`--dry-run` but no `--no-verify`). If validation fails the command refuses
with a structured diagnostic and **does not modify any file**.

For these edit commands, a JSON result of `gate: "passed"` means the
post-edit source-claim, assignment, and realizability checks passed. It does
not mean the full reconstruction was accepted: `debundle run` remains the
end-to-end acceptance path and checks vendor boundaries, exports,
reconstruction, and browser planning. The source-claim check recognizes named,
default, and namespace imports as import specifiers, while module emission
currently supports moving named imports only (`lowering/imports/imports_runtime.rs`).

On spec-edit commands:

| Flag          | Effect                                                                                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (default)     | Validate; refuse the change if invalid; apply if valid.                                                                                                       |
| `--no-verify` | Skip validation; apply the change regardless. Escape hatch for multi-step refactors where an intermediate state is intentionally invalid. Don't use casually. |
| `--dry-run`   | Validate (or simulate) but do not modify any file. Print the validation result and the planned file set, not a YAML diff.                                     |

`--dry-run` and `--no-verify` can be combined: show what would change
without validating — useful when investigating _why_ the gate would reject.

`run` is the exception: the gate is part of the pipeline contract, not an
optional pre-check. There is no `run --no-verify` — if you want the pipeline
to emit JS regardless of the gate, fix the spec first.

Read-only commands take neither flag — they have no side effects.

## Running the pipeline

```bash
debundle run --spec <transform-spec.yaml>

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir>
```

If the spec is unrealizable, `debundle run` rejects and emits structured
side outputs (`cycles.json`, `atomic_group_conflicts.json`) under
`reports/tree/<chunk-id>/`. `debundle run --dry-run` runs pipeline
parse/facts/gate checks without writing emitted JS or reports; a gate
rejection still writes `dependency_graph.json` plus the rejection evidence, so
the `gate` queries work on the rejection that was just reported.

Selector failures follow the keep-going (default) and `--fail-fast` modes of
<../SPEC.md> § Modes, in the order of `docs/selector_resolution.md` § Order. Bulk
selector edits keep going, reporting every failure of a pass in each chunk's
`selector_diagnostics.json`; use `--fail-fast` only when the first failing
selector or claim is the useful debugging target (its outcome line is the
error).

Every chunk's selectors resolve as one program. In a tree spec, several
module trees may scope to one chunk (`module_roots`, <../README.md>); their
entities never claim the same place.

`debundle spec validate` is `debundle run` in dry-run keep-going mode
reporting every selector problem: it takes the **same inputs** (`--spec` /
`--tree-config` + package roots) and runs the full pipeline, not only the
modules tree. Its source-only
preflight mode (`--modules` plus `--source-file` or `--source-root
--chunk`) needs no pipeline build and resolves the module files jointly with
the same resolve as `run`. How its outcomes differ from the pipeline's:
<../SPEC.md> § Outcomes; also, a name pin on a binding the chunk does not
declare is `no_match` in this mode but an unmatched claim in `run`.
`validate` prints the complete report, then exits nonzero if any outcome has
error severity. Warnings do not fail validation. Pass `--report-only` to keep
the report but exit zero for selector errors, for example when collecting
diagnostics across several chunks.

Each group of interacting selectors is one CP-SAT solve inside the `debundle`
process (<selector_resolution.md> § The solver) — in `run`, `validate`, and in
the edit gate, `describe` and `peel` when they resolve source claims. The
binary needs nothing beside it. `match-selector` and `synthesize-selectors`
resolve one selector at a time and never solve.

### Inspecting source

`debundle inspect-source` parses source with the same SWC parser used by
`spec match-selector`. It can select numbered top-level items directly from a
file, or resolve a graph/spec ID to its source units:

```sh
debundle inspect-source --source-file app.js
debundle inspect-source --source-root extracted --chunk static/app.js \
  --statements 12..16 --format json
debundle inspect-source --source-file app.js --around-binding XOe
debundle inspect-source --id atomic:17 --graph dependency_graph.json --modules spec/modules \
  --source-root extracted
debundle inspect-source --id runtime/plugins --view raw --context-statements 1
```

`--statements` accepts one zero-based index or an inclusive `START..END`
range. With no selector it starts at the first item and prints up to the
configured statement limit. `--around-binding NAME` locates
the unique top-level item that declares that name and includes two neighboring
items on each side by default; set `--context-statements` to change that count.
The range, binding, and ID options are mutually exclusive. A missing or
repeated top-level declaration is an error. `--id` accepts the same IDs as
`describe` and requires `--graph` and `--modules` (or their environment
variables). It shows each selected raw item separately, even if selected
source units are far apart. The graph's post-split source-unit ordinals are converted to
raw `body[N]` indices; a multi-declarator item can contain several source units.
Graph/source location mismatches are errors, not silent substitutions.

The labels are raw parsed `Module.body` indices. They use the same `body[N]`
coordinate that structural selectors report, but they are not source-unit IDs.
Direct source-file selection does not require a spec or dependency graph. Each
result includes its half-open UTF-8
byte span in the original file, one-based byte line/columns (end exclusive),
declared top-level binding names, selected source-unit IDs when applicable, and a
source snippet. `--view pretty` (default) uses SWC's readable rendering;
`--view raw` uses the original source bytes before truncation. The chosen view
applies to text and JSON. By default, the command shows at most 32 items,
512 bytes per source line, and 32 KiB of serialized output. It reports omitted
items and source bytes. Set `--max-statements 0`, `--max-line-bytes 0`, or
`--max-output-bytes 0` to disable the corresponding limit explicitly.
Text is the interactive default; pipes default to JSON.

### Selector outcomes

`run --dry-run` (per chunk, `reports/tree/<chunk-id>/selector_diagnostics.json`),
`spec validate` and `spec match-selector` all report selectors as
`SelectorOutcome` records (`selectors/resolution/selector_outcome.rs`; kinds and severities in
<../SPEC.md> § Outcomes). JSON is `{counts, outcomes}`; `run` and
`validate` list only selectors that did not resolve plus warnings,
`match-selector` its one probe (with `slack`). Text is one line per record:

```text
[ambiguous] static/app::ui/panel as `Panel` (source_matches[].bindings[`p`]): is ambiguous -- matched 2 top-level declarations: `a` (body[0]), `b` (body[1]) -- selector: const p = renderPanel(ANYTHING);
```

```json
{
  "counts": { "ambiguous": 1 },
  "outcomes": [
    {
      "chunk": "static/app",
      "placement": {
        "logical_module": "ui/panel",
        "entity": { "export": "Panel" },
        "selector_kind": "source_matches"
      },
      "target_binding": "p",
      "selector_preview": "const p = renderPanel(ANYTHING);",
      "outcome": {
        "kind": "ambiguous",
        "candidates": [
          { "body_index": 0, "binding": "a" },
          { "body_index": 1, "binding": "b" }
        ],
        "truncated": false
      },
      "severity": "error"
    }
  ]
}
```

`body_index` is the matched statement's index in the chunk body.

A `no_match` of a one-statement template also lists `nearest_unclaimed`: the
statements no entity claimed that it comes closest to, each with its body
index (`body_index`), the `bindings` it declares, a `score` (higher is closer) and
the first place it diverges. After an upstream bump, the first entry is
usually the code the selector should now match:

```text
[no_match] app.js::widgets/widget as `Widget` (source_matches[].bindings[`Widget`]): did not match any top-level declaration; nearest unclaimed: body[0] declaring `a` (score 70): selector class pinned member `shut` was not found in the candidate class body in order; body[1] declaring `b` (score 70): selector class pinned member `open` was not found in the candidate class body in order -- selector: class Widget { open() { STMT_LIST; } shut() { STMT_LIST; } }
```

An `unsatisfiable` outcome means an interacting group admits no joint
assignment. Its `group` identifies the component by its first entity. When
available, `witness.selectors` names competing claims and `witness.source_units`
lists their complete candidate source-unit union. Text distinguishes participants
in that conflict from other group members blocked by it. A group without a
witness remains infeasible; the diagnostic explicitly reports that no smaller
ownership witness was found within its limits.

`validate` also lists, under `templates`, what each free identifier of every
matched template (member, `source_matches[]` entry or anonymous statement)
means (<../SPEC.md> § Matching): a `reference` with its `entity`, a `global`,
an `ambiguous` name with the `modules` exporting it, or a `wildcard`. The text
output counts them, then prints one line per reference and ambiguous name:

```text
2 matched template(s) with free identifiers: global=1, reference=1, wildcard=3
  - static/app::ui/panel `Panel`: `Widget` references `Widget` in ui/widget
```

`validate --format ndjson` streams one line per outcome (`section: outcome`),
then one per template (`section: template`), then a `summary` line with the
counts.

## Batch atomicity (`bindings assign`)

`bindings assign` takes one or more positional triples
`<sym>:<module>[:<readable>]` and/or `--batch` JSON. Validation runs on the
post-batch spec, not after each individual assignment — so multi-binding
refactors whose intermediate states would be invalid can land in one shot:

1. Parse all moves. Resolve each `sym` to its member and dedupe on the
   resolved identity; contradictory moves for one member are rejected,
   duplicates collapse with a stderr warning.
2. Compute the post-batch spec in memory — parsed through the same claims
   model `debundle run` loads, so `members:`, `source_matches:`,
   `annotations:`, and `anonymous_statements:` in every module stay
   claimed. Moving a `source_matches[].bindings[]` entry carries its
   structural template and claim note. Selecting only some bindings from a
   claim splits the claim; move atomic siblings together in one batch when
   the gate reports that a single move would split their source statement.
3. Run the realizability gate on the post-batch spec.
4. If invalid: print binding-pair blame, exit non-zero, **do not modify any
   file**.
5. If valid: write every changed surviving YAML first, then delete drained
   move-source files — an interruption can never lose a member that was not
   yet written to its destination.

`--dry-run` runs steps 1–4 and stops. `--no-verify` skips step 3 (still
does duplicate-claim detection — that's a structural error, not a
validation one). `bindings unassign` shares the same post-batch validation
and drain sweep. Automatic cleanup preserves module-level `comment:` and
`note:` fields, including explicitly empty values. `modules list --auto-deletable`
uses the same metadata-preserving predicate; `--empty` only tests structural
emptiness. Explicit `modules delete <path>` may remove a comment/note-only module
without `--force`, because it owns no code and the author named it for deletion.

### `bindings assign --batch` JSON format

A top-level JSON array of move objects (`sym` and `module` required;
omitting `readable` preserves the current readable name):

```json
[
  { "sym": "XOe", "module": "runtime/plugins", "readable": "PluginSettingsAccessor" },
  { "sym": "YOe", "module": "runtime/plugins" }
]
```

`--batch` also accepts `modules propose --format json` output, or a
top-level array of proposal objects selected from `.proposals`, when every
selected binding is already claimed in YAML and each row is a direct move:

- `landable_today: true`
- non-empty `binding_ids`
- no `merge_into`
- no `anonymous_statement_source_unit_ids`

For fresh residual bindings, use `modules apply` below. Existing-claim proposal
rows move each `binding_ids[]` entry to `proposed_module_id` or
`extends_module_id`. Proposal rows carry no `readable`; rename with a move
array. The rest are rejected:

- `merge_into` rows combine existing source modules: use `modules merge` or
  manual YAML.
- Rows with `anonymous_statement_source_unit_ids` need `anonymous_statements:`
  edits, which `bindings assign` does not perform.
- Rows with `status: blocked_residual_dependency` (`landable_today: false`)
  read other residual classes (`other_residual_classes_referenced`), so promoting
  one alone would trip the realizability gate: assign it together with the
  classes it references, or co-locate the source units manually first.

## Claiming fresh proposals (`modules apply`)

For an app-level spec with several chunks, pass the same `--tree-config`
to `modules propose` and `modules apply`. The config's `module_roots` maps
the graph's chunk ID to its authored YAML subtrees. `modules apply` checks
only those subtrees against the chunk graph and preserves each configured
root's exact filesystem spelling; it normalizes only the logical module
suffix. A destination outside the mapped roots is rejected. Single-chunk
specs can omit the flag when `--modules` already names that chunk's tree.

`landable_today` means the proposed source-unit set has no outgoing residual-class
dependency and its anonymous source units are addressable. It does not guarantee a
structural selector can be synthesized or that the final edit passes the gate.
`modules apply` claims reviewed residual bindings with proven `source_matches:`
selectors. It requires `--graph`, `--modules`, and `--source-root` (or their
environment variables), and accepts a JSON batch of this shape:

Bindings from one multi-declarator statement are synthesized as one shared
selector. References between selected declarators use their requested readable
names in that selector.

```json
[
  {
    "proposal_id": "auto_partition_0007",
    "source_unit_ids": ["unit-id-from-the-reviewed-row"],
    "module": "services/provider",
    "readable_names": { "a": "Provider" }
  }
]
```

Copy `proposal_id` from `proposed_module_id` and `source_unit_ids` from the same
current `modules propose --format json` row. Choose the destination and a
readable name for every residual binding in that row. For extension rows the
destination must equal `extends_module_id`. Use the same `--size-cap-lines`
value as `modules propose` if it was overridden. The command rejects stale
source-unit sets, merges, anonymous-statement rows, unprovable or name-pinned selectors,
and failed post-edit gates before writing any YAML. `--dry-run --format json`
reports each selector in `selector_reviews`, including its source-match claim
or relational selector, provenance, and rebuild-anchor review warning. It
reports planned files without writing the spec. Uniqueness is proved against
the current source snapshot; review the retained literals and property names
before applying. Omit a proposal from the batch to leave it in residual.

Optional `selectors` entries choose a reviewed selector for a binding; bindings
without an entry use synthesis. A `source_match` entry uses the normal
`source_matches:` claim shape and must name exactly one binding. A `relational`
entry uses the normal `members[].selector` shape (for example `cross_ref` or
`reads_member`); name pins are rejected. Each chosen selector must resolve to
the requested source binding, not merely to some unique declaration:

```json
{
  "proposal_id": "auto_partition_0007",
  "source_unit_ids": ["unit-id-from-the-reviewed-row"],
  "module": "services/provider",
  "readable_names": { "a": "Provider" },
  "selectors": {
    "a": {
      "kind": "source_match",
      "selector": {
        "match": "const Provider = { value: 4 };",
        "bindings": [{ "local": "Provider", "name": "Provider" }]
      }
    }
  }
}
```

## Refining existing modules (`modules decompose`)

`modules decompose` reports a read-only candidate that splits existing logical
modules into the surviving gated same-module seed fragments:

```sh
debundle modules decompose --graph dependency_graph.json --modules spec/modules \
  --format json
```

It keeps existing modules separate, covers every source unit in the candidate, checks
that structural atomic groups stay together, and runs the shared realizability
gate over the complete candidate. `candidate_is_realizable: true` means that
candidate passed those checks. A false result includes the gate's cycles or
cross-module rebinds and any structural atom conflicts; the fragments remain
diagnostic suggestions and do not form an accepted assignment. By default
residual source units keep their current destination and are not split into
suggestions, so a candidate can remain invalid when its conflict involves
residual ownership.

By default `modules` contains only existing modules that split into multiple
fragments. Pass `--include-unchanged` to also report current named modules that
the selected strategy leaves intact. Each is one fragment with its original
module path and the source-unit and binding IDs needed to include it in a
reviewed `modules refine` batch. This option does not change
`unchanged_module_count` or add intact residual/entry groups.

The command does not modify YAML. Review the reported source-unit and binding IDs,
choose semantic module names for each suggested part, and assign the complete
set of fragments together. Suggested `part-N` paths are temporary review aids,
not names inferred from the source. This is a conservative seed refinement,
not a minimal module assignment: its canonical pivot order can leave mutually compatible
source units in separate fragments. Unlike `modules propose`, decomposition has no
module-size cap and does not run the greedy cross-module proposer.
Some fragments may include source units without binding IDs, such as anonymous
statements; inspect those source units and move them with their `anonymous_statements:`
selectors when applying the reviewed split.

For a faster, more fragmented baseline, pass `--atomic`. This mode skips
same-module seed contractions and creates one candidate group per structural
atomic group. If a unit already spans existing module labels, its source units keep
those labels and the report remains false with that atom conflict listed. Both
modes run the same full candidate gate; JSON's `strategy` field identifies the
selected mode.

`intrinsic_alias.referenced_by` selectors resolve their anchor within the same
logical module. A `source_match` template can also use a free identifier that
resolves to a member claim in its own module; moving that capture away can make
the reference ambiguous when more than one member exports that readable name.
When the module-neutral resolver finds one export, it can still resolve the
reference after the capture is split away.
To preserve both kinds of local relationship while splitting, opt in to
source-backed selector resolution:

```sh
debundle modules decompose --atomic --preserve-selector-anchors \
  --source-root source --graph dependency_graph.json --modules spec/modules --format json
```

This option requires `--source-root` and resolves current claims through the
normal joint selector resolver. It coalesces each member with its local
`intrinsic_alias` anchor and each source-match capture (a named member or an
anonymous statement selector) with local members whose free names are ambiguous
under module-neutral lookup. The JSON report sets `preserve_selector_anchors`
and lists each relationship in `selector_anchor_relations`. Each relation has
`kind`, `module_path`, `source`, `referenced_by`, `source_unit_ids`, and
`anchor_source_unit_ids`. `source` is tagged as either
`{"kind":"member","name":"Widget"}` or
`{"kind":"anonymous_statement","index":1}`; the latter index is the
authored `anonymous_statements[]` selector index. References with one
module-neutral target retain their normal resolver behavior. The command
remains read-only, and the complete candidate still passes through the same
structural and realizability gates. Without the option, graph-only
decomposition behavior is unchanged.

To ask for a coarser candidate when the atomic-group assignment is blocked by gate
cycles, add `--coarsen-blocking-sccs`:

```sh
debundle modules decompose --atomic --coarsen-blocking-sccs \
  --graph dependency_graph.json --modules spec/modules --format json
```

This opt-in mode consumes only the blocking SCC module lists returned by the
shared gate. It co-locates the candidate groups in each reported SCC, then runs
the full structural and realizability checks again. It repeats while blocking
SCCs remain, stopping when the complete candidate passes or when atomic-group
conflicts, cross-module rebinds, or lack of progress prevent another safe merge.
If an SCC contains the residual entry, the merge keeps that entry path, so its
top-level import and export declarations remain there.

The JSON `modules` rows still describe the initial atomic fragments. The
`coarsening` object reports each consumed SCC, the final candidate groups, the
original module paths represented by each group, and the stopping reason. The
top-level coverage, cycle, rebind, and `candidate_is_realizable` fields describe
the final candidate. A passing result is a gate-accepted assignment, not a claim
that groups sharing a suggested path belong to one semantic feature. The mode
does not edit YAML or choose descriptive module names; review `origin_module_paths`
and source-unit IDs before assigning the groups in a later spec change.

When structural atoms span candidate module paths, SCC coarsening normally
stops with `atomic_group_conflicts`. Add `--coarsen-atomic-conflicts` to first
co-locate the complete candidate groups named by each reported atomic group:

```sh
debundle modules decompose --atomic --coarsen-blocking-sccs \
  --coarsen-atomic-conflicts --graph dependency_graph.json --modules spec/modules \
  --format json
```

Overlapping atomic conflicts are joined transitively. A merge containing the
residual entry keeps its entry path, including import and export declarations.
The complete candidate goes through the shared gate before SCC coarsening and
again after each SCC round. The JSON `coarsening.atomic_conflict_merges` rows
give the native unit IDs, previous candidate paths, and new path; final
ownership is in `coarsening.groups`. This flag requires
`--coarsen-blocking-sccs`. Without it, reported structural conflicts stop
coarsening as before.

With `--preserve-selector-anchors --source-root`, the final `coarsening` report
also includes `naming_collisions`. Each row names a suggested destination and
readable export name that distinct original modules would both claim there,
with each resolved source binding and its source-unit IDs. It covers direct
members and `source_matches` after the joint selector resolution, and reports
all such collisions in the final coarsened groups. An empty array means this
particular name check found none; the read-only planner does not rename members
or certify that a later spec edit passes every source-level check. Graph-only
decomposition omits this field.

To retain as many original semantic homes as the accepted assignment permits,
add `--rejoin-original-modules` to the SCC coarsening command. This requires
`--coarsen-blocking-sccs` and starts only after its complete candidate passes
the shared gate. It contracts whole candidate groups when every source unit in both
groups came from the same original module. Each proposed contraction passes the
full shared realizability gate on its hypothetical complete assignment. Groups
that already combine different original homes remain intact. The final complete
assignment passes that gate again. A fully rejoined home gets its original path;
other groups keep deterministic temporary paths.

For a bounded authoring pass, add `--rejoin-pair-attempt-limit N`. This requires
`--rejoin-original-modules` and limits speculative partial pair checks per
original home across all passes. A zero limit still tries each whole-home
contraction, including two-fragment homes. The default has no limit. The report
lists `budget_limited_home_count` and `budget_limited_home_paths`; a
`pair_attempt_limit_reached` stop reason means some homes still have untried
pairs. A limited proposal still passes the same complete final gate.

To bound retries across homes, add `--rejoin-home-pass-limit N`. This also
requires `--rejoin-original-modules` and limits complete stable-order passes
over pure original homes. Zero skips rejoining; one runs at most one pass. The
report gives `whole_home_pass_count` and `whole_home_pass_limit`; when the cap
stops a productive pass while pure homes remain split, the stop reason is
`whole_home_pass_limit_reached`. The default remains exhaustive; limits never
bypass either gate check.

The `coarsening.original_home_rejoining` report lists fully restored and
`unrestored_module_paths`, attempted and accepted contractions, final group
count, and largest group size and origin count. An unrestored home may be split
across groups or wholly absorbed into a mixed-origin SCC group. The option has
no module-size cap, so an existing large home can rejoin. It remains read-only:
review the reported final `coarsening.groups` before applying assignments with
`modules refine`.

To include residual ownership in that baseline, add `--include-residual`:

```sh
debundle modules decompose --atomic --include-residual \
  --graph dependency_graph.json --modules spec/modules --format json
```

Each residual body atom gets its own proposed active module. Top-level import
and export declaration atoms remain at the residual entry destination so the
normal emitter can keep their module scaffolding in the entry. Atoms spanning
different current destinations also keep those destinations and remain visible
as conflicts. The complete candidate still goes through the shared gate; an
atomic split is not assumed to be realizable. The suggested
`decomposition/residual/part-N` paths are temporary names that authors should
replace after reviewing the source.

## Applying reviewed source-unit assignments (`modules refine`)

Use `modules refine` to apply a reviewed source-unit-to-module batch to claims already
present in the scoped module tree:

```sh
debundle modules refine --batch reviewed-source-units.json \
  --graph dependency_graph.json --modules spec/modules --source-root source
```

The batch is a JSON array of `{ "unit_id": "...", "module": "..." }`
rows. `module` names a destination path; with `--tree-config`, include the
selected chunk root in that path. Source units omitted from the batch keep their
current assignment. Each row moves every existing selector that resolves to
that source unit, including `members:`, grouped `source_matches[].bindings[]`, and
`anonymous_statements:` entries. It preserves selector text, binding names,
annotations, source-match notes, and anonymous-statement comments and notes.
Module-level `comment:` and `note:` fields stay with their original module.

The command accepts only already-claimed, non-residual source units. Residual,
unclaimed, import, and export source units cannot be assigned. It rejects duplicate
source-unit rows, split/conflicting current claims, destination name or annotation
collisions, and stale dependency graphs. It then resolves every selector in the
complete scoped post-edit spec and compares every graph source unit—including
unchanged and residual source units—to the requested complete assignment. Only after
that identity check does the shared atomic-group and realizability gate run.
There is no `--no-verify` mode. `--dry-run` performs the same checks without
writing YAML; a rejected batch leaves every module document unchanged.

The primitive does not create selectors or choose semantic module names. It
applies the reviewed source-unit map as written; inspect the resulting modules and
run the project extraction gate after an accepted edit.

## Rejection diagnostics

When validation refuses a mutating command, the diagnostic names exactly
what's wrong:

**Atom split** (refused by the realizability gate). Lists each split atom:
which source units it covers, which modules its members would land in, and the
`DepKind` causes from the group (same data shape as `AtomicGroupConflict`).
When a sequenced side-effect edge is involved, the existing conflict/cycle
records also include each impure initializer source unit, its binding name, source
path and 1-based line/column, plus the `PurityRule`, optional detail, and any
author guidance. Unknown calls point to the member-level `purity` annotation;
imported fluent chains point to `chunk_export_purity.<chunk>.fluent_exports`.
Unknown imported constructors point to
`chunk_export_purity.<defining chunk>.pure_new_exports`.
The diagnostic does **not** auto-compute the minimal extra-moves completion
— it names the source units and destinations so the author can read the
completion off the printed atom membership.

**Name collision** (refused by `bindings rename` or by `bindings assign`
when a `:readable` field collides). Lists each collision: the existing
binding holding the name, the binding the rename would have given it.

Both diagnostic shapes go to stderr; the command exits non-zero. Under a
JSON format a realizability-gate rejection additionally prints one
structured object on stdout: `{verb, action: "rejected", rejection}`, where
`rejection.kind` is `atom_split` (with `conflicts`, the canonical
`AtomicGroupConflictReport` projection) or `unrealizable_cycles` (with
`blocking_sccs`, the canonical `BlockingSccEntry` projection). These are
the **same wire shapes** `atomic_group_conflicts.json` / `cycles.json` carry
— there is no parallel rejection schema.

Edit-gate rejections (including `--dry-run` probes) also write those
artifacts to disk as siblings of `--graph` — the default location the
`gate` queries read — so the documented follow-up queries work on the
rejection that was just reported. A subsequent edit that passes the gate
clears the stale artifacts. `debundle run` (and `run --dry-run`) writes the
same files under `reports/tree/<chunk>/`, which is the same
sibling-of-`dependency_graph.json` layout.

## Gate queries

The `gate` namespace names what is rejecting: the realizability gate. It
complements `scc` (which lists every quotient SCC, including singletons and
realizable multi-node ones); `gate` lists **only** the SCCs the gate
rejected. The unit is the blocking SCC, not a cycle — a single SCC can
contain exponentially many simple cycles, so the CLI exposes the cut (a
primitive on the SCC) but deliberately not a `cycle list`.

Each `gate ...` command accepts `--cycles <path>` to override the default
`cycles.json` location (sibling of `--graph`). A missing `cycles.json` is
the clean state: zero blocking SCCs, exit 0.

## Workflow: investigating a binding end-to-end

When a binding's role is unclear or a proposal is suspicious:

1. **`debundle describe <sym>`** — graph + spec context: the binding's
   source unit, home module, atom membership, and incoming/outgoing edges. Add
   `--include-proposals` only when proposer proposal/diagnostic
   annotations are needed.
2. **`debundle inspect-source --id <sym>`** — inspect the source unit's numbered
   source item. Use `--view raw` for original bytes and
   `--context-statements 1` for neighboring items.
3. **`debundle cluster <sym>`** — list the module-quotient neighbors of the
   binding's source unit. Useful for "what does this module touch?" questions
   before deciding a destination.

## Evidence files

Typical debundle outputs:

- executable JS under `app/`
- root reports under `reports/`: `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, `vendor_swaps.json` when those outputs are
  configured
- per-chunk reports under `reports/tree/<chunk-id>/`: `chunk.json`,
  `modules.json`, `dependency_graph.json`, `export_reentry.json`,
  `selector_diagnostics.json` when any selector did not resolve or resolved
  with a warning, plus `cycles.json` / `atomic_group_conflicts.json` only when
  validation rejects. `export_reentry.json` is also kept on a dry-run
  rejection and omitted on an accepted dry run.
- mirrored per-directory and per-file dependency reports under
  `reports/tree/**/index.json` and `reports/tree/**/*.js.json`

Use manifests for progress reporting rather than rescanning generated JS by
hand. Tree reports carry hierarchy-health evidence (semantic dependency
counts by kind, symbol/file attribution for boundary crossings); treat them
as graph evidence to pair with source reading, not as a substitute for
understanding the implementation.

## Gate discipline

The adapter-provided gate is authoritative. For suspicious green builds,
force a fresh execution; do not trust cache-only success when validating
new module boundaries. Generated JS conflicts are resolved by the adapter
regen command, not by hand-editing generated output.

## Comments

The `comment:` / `note:` schema, emission and move semantics, and editing
workflow are in <spec_editing.md> § Comments and notes. The CLI surface is
`bindings comment` / `modules comment` — binding edits address
`annotations.<name>.comment` for
both member and source-match bindings; module edits address the top-level
`comment:`. Non-emitting `note:` fields are YAML-authored metadata.

## Out of scope

- **No cross-process materializer reader.** `debundle run` reads the spec
  and emits JS in one process — see `wire_format.md` § "Why pre-filter
  facts (`StatementFacts`) aren't on the wire" and
  `lessons_learned/cross_process_stage_b.md`.
- **Shell tab completion.**

## See also

- `design.md` — the realizability theorem the gate enforces; § "Layered
  mental model" + § "Factor assembly inside `debundle run`" for the
  factorization algorithm `modules propose` draws from.
- `wire_format.md` — JSON sidecar conventions readers of these commands
  consume.
- `selectors.md` / `spec_editing.md` — selector authoring and spec-editing
  workflows.

## Explain a selector at a known source range

`spec match-selector --explain --statements START..END` performs a local
structural comparison using the labels from `inspect-source`. Supply `--match`
inline or `--selector 'modules/file.yaml#/source_matches/0'` to read an authored
entry. This mode does not run the whole-spec solver, establish uniqueness, or
check external reference identities. The debugging workflow and address syntax
are in <debugging_selectors.md>.
