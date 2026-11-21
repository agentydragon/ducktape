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
JSON-on-stdout / readable-diagnostic-on-stderr convention.

## Find a workflow

To inspect original static ESM loading paths before a spec passes validation,
run `debundle static-imports` with the same flat-spec or tree inputs as `run`:

```bash
debundle static-imports --spec spec.yaml --from main \
  --target dependency --target other --format json
```

The command reports strict request-edge paths from `main` to each target,
back to `main`, and from each target back to itself. A null witness means no
path through resolved ordinary static ESM requests; `request_issues` identifies
unresolved, nonordinary, or nonentry requests in the inspected closures.
`--max-issues` limits displayed issues while `request_issue_count` retains the
total. This is source-graph evidence, not a cache or runtime verdict.

Add `--include-host-loader-candidates --interop-census` to include all direct
literal `import.meta.require(...)` calls as conditional graph edges and report
typed interop sites across every chunk. Calls inside deferred functions are
included in the union graph. Direct loader-value references, dynamic direct
targets, global loader forms, and CommonJS export uses remain visible as sites
without graph edges; calls through an alias are not traced. The command
does not assert the original host-loader identity or clear runtime blockers.
`--max-interop-sites` limits detailed site rows while per-chunk kind counts
remain complete.

The README's [task chooser](../README.md#choose-a-task) links to the recipe for
inspection, extraction, reorganization, naming, selector stabilization, gate
diagnosis, or delegation.

Use `debundle <command> --help` for the current command and flag inventory.

## Delegating with an input snapshot

Generate current analysis, then create a separate snapshot for each worker:

```bash
debundle snapshot create --graph output/reports/tree/main/dependency_graph.json.gz \
  --destination /tmp/worker-one > /tmp/worker-one.json
debundle snapshot verify /tmp/worker-one
```

`--graph` may also come from `DEBUNDLE_GRAPH`; an explicit flag wins. The
returned JSON identifies the copied engine, graph (with the `program_graph.json.gz`
it references), spec/source roots, complete
modules tree, `graph_package_roots` map and a `regeneration_argv` array. For a
graph-backed query or edit in the snapshot, pass each map entry as the global
`--graph-package-root NAME=DIR` alongside `--spec-root` and
`--graph-source-root`. The reserved `__packages_root__` entry relocates the
shared `--packages-root` directory. Execute `regeneration_argv`
directly. The replay uses only copied inputs and writes under that worker's
`output/` directory. To display a shell command:

```bash
jq -r '.regeneration_argv | @sh' /tmp/worker-one.json
```

Send the worker its snapshot directory, task, acceptance checks and permitted
edit scope. It edits its own YAML tree and regenerates analysis there. The
original saved graph and `snapshot.json` describe the starting state; the
snapshot baseline check will fail after edits. Subsequent graph-backed edits
use the newly generated graph and copied roots. Create another snapshot for
each concurrent worker instead of sharing writable spec paths.

Return the reviewed YAML patch or changed files/deletions, the starting
snapshot manifest, and exact validation invocations/results. Before integrating,
verify that the receiving spec still matches that baseline. Apply reviewed
changes in an isolated integration checkout, regenerate the combined spec and
run its full pipeline/runtime checks. Individually accepted edits can conflict
when combined.

Snapshot creation currently supports tree-shaped specs with relative embedded
source/vendor-bundle paths. It refuses unrooted input dependencies and existing
destinations. Relocation roots are supplied by the global `--spec-root` and
`--graph-source-root` options when the original graph was generated elsewhere.

## Common arguments and env vars

Most commands take these paths, each as a flag or an env var; the flag wins if
both are set.

| Flag                            | Env var                      | Meaning                                                                                                                                                                                                    |
| ------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--graph <path>`                | `DEBUNDLE_GRAPH`             | `dependency_graph.json.gz` for the chunk being inspected or edited. The graph path implies the chunk; multi-chunk callers point at different graphs per invocation.                                        |
| `--modules <dir>`               | `DEBUNDLE_MODULES`           | Per-module YAML tree root (the directory under `spec/modules/`).                                                                                                                                           |
| `--source-root <dir>`           | `DEBUNDLE_SOURCE_ROOT`       | Selector lookup root. It must contain the graph's verified physical input; an existing logical-path candidate must resolve to that same file. Missing alias paths use the captured chunk-to-input mapping. |
| `--tree-config <file>`          | `DEBUNDLE_TREE_CONFIG`       | For graph-backed `modules` edits and queries, including `describe`, scope an app-level modules tree to the chunk named by `--graph`.                                                                       |
| `--spec-root <dir>`             | `DEBUNDLE_SPEC_ROOT`         | Current common root of the tree config, vendor marks and modules after relocation. Global flag; the flag wins over the env var.                                                                            |
| `--graph-source-root <dir>`     | `DEBUNDLE_GRAPH_SOURCE_ROOT` | Current root of the analyzed input files after relocation. Distinct from `--source-root`, which resolves selector source paths. Global flag; the flag wins.                                                |
| `--graph-package-root NAME=DIR` | —                            | Current root for a package input in the saved graph after relocation. Repeat for each relocated package. Global flag.                                                                                      |

Graph-backed spec queries and edits compare the saved graph's engine and
captured input bytes and YAML path membership with the current roots before
using the graph. The graph records both a logical chunk source path and the
physical input file whose bytes were parsed. Source-backed selectors keep the
logical path as their parser and graph identity while using the verified
physical input bytes. This supports JS-list aliases such as
`virtual/chunk.js` backed by `snapshot/provider.js` without accepting an
unrelated same-byte copy. `--source-root` is a consistency boundary for
selector lookup: the verified input must be inside it, and if the logical path
exists there it must identify that exact input. It does not relocate graph
inputs. Use `--graph-source-root` to explicitly relocate captured physical
inputs, preserving each path relative to the recorded graph source root; the
selector root, when supplied too, must contain the relocated input.

Plain `graph-summary` reads the saved report without verifying the current
engine, source, packages or YAML. Its success establishes report readability;
supplying relocation flags does not turn it into a compatibility check. For a
bounded replay preflight, use `inspect-source --id unit:N` with the matching
graph and roots, or `describe` with the graph's module scope. Retain the
command's successful exit status and source report before claiming source
compatibility.

`--modules` must name the graph chunk's exact configured
subtree. Commands accepting `--tree-config` may name the full modules tree
when that flag names the config captured by the graph. `describe` resolves
module paths and binding homes using only all YAML roots assigned to that
chunk; those roots' claims resolve together through the shared selector
resolver. A changed source,
YAML, config, dependency assumption, wrong chunk tree, or old graph without
provenance exits nonzero. JSON and NDJSON commands emit a typed
`graph_compatibility` rejection with `mismatch.reason`, expected/actual
values, and `regenerate_command` argv. A successful edit leaves the saved
graph describing the pre-edit spec; rerun `debundle run` before another
graph-backed edit. With explicit relocation roots, the mismatch recipe uses
the current engine and translated input paths, and `regenerate_cwd` names the
current spec root. The recipe selects an unused `-regenerated` sibling of the
translated output directory, adding a numeric suffix if needed. If the recorded
output root has no supplied relocation root mapping and `--spec-root` is
supplied, it instead selects an unused `debundle-output-regenerated` directory
under the current spec root, with a numeric suffix on collision. If no current
spec root is supplied, an unmapped output uses an unused `-regenerated` sibling
beside the recorded output, with a numeric suffix on collision. Computing the
recipe does not create or remove the selected directory.
Flat-spec graphs have no authored module-tree scope and cannot authorize a
YAML-tree edit or query. `inspect-source --id unit:...` can inspect one saved
source unit from a flat graph: it does not resolve module claims, but still
checks the captured engine and input fingerprints plus the exact source path
and digest before loading source bytes. Other `--id` selections retain the
module-scope check.
`--no-verify` skips the post-edit assignment gate only; it does not skip
the saved graph's baseline compatibility check when a graph is supplied.

`debundle run --out-root` reads `DEBUNDLE_OUT`. `debundle run
--tree-source-root` (the spec-tree compile root for source-relative paths in
the tree-shaped config) reads its own env var, `DEBUNDLE_TREE_SOURCE_ROOT` —
deliberately **not** `DEBUNDLE_SOURCE_ROOT`, because the two roots are
different directories in real corpora (spec tree vs. upstream snapshot).

Export the env vars once per shell session and subsequent commands run
without repeating the flags:

```bash
export DEBUNDLE_GRAPH=<debundle-output>/reports/tree/<chunk-id>/dependency_graph.json.gz
export DEBUNDLE_MODULES=<matching-chunk-modules-subtree>
export DEBUNDLE_SOURCE_ROOT=<captured-source-lookup-root>
export DEBUNDLE_OUT=<debundle-output-root>

debundle describe XOe
debundle scc --binding XOe
debundle bindings assign XOe:runtime/plugins
```

Use the matching captured input root for source lookup. Obtain its path and the
chunk's module scope from the project adapter or verified snapshot; emitted
`app/` is not the captured source root.

## Resolved invocation records

Global options capture a JSON `ResolvedInvocation`:

| Option                    | Behavior                                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `--print-invocation`      | Validate CLI shape, resolve flags/documented env values/defaults, print the record and exit without dispatch. |
| `--invocation-out <path>` | Write the record before dispatch, preserving normal stdout/stderr; retain it even if the operation fails.     |
| Both                      | Write and print the record, then skip the operation.                                                          |

Resolution reads CLI metadata, cwd and executable path, not graph/source contents
or unrelated ambient environment. The record contains:

- `argv`: executable and resolved options; env-backed paths such as `DEBUNDLE_GRAPH`
  become explicit flags, with CLI-over-env precedence. Relative paths remain relative
  to the recorded cwd, enabling replay after caller environment changes.
- `shell_command`: POSIX-quoted arguments plus a change to that cwd.
- Effective arguments and their value sources, operation class, `requested_proof`
  and `write_scope`. The latter lists possible writes, including the invocation file,
  rather than files actually changed.

Classes distinguish graph/diagnostic/source snapshot reads, fresh pipelines,
snapshot creation/verification, validated/unverified edits, conditional comment
operations and read-only analysis. Other shapes remain `unclassified`. Requested
checks are not completed checks: an edit gate establishes neither fresh pipeline,
reconstruction nor runtime success; saving a record does not prove success.

Records neither fingerprint the spec/source/graph/other file inputs nor pin executable
contents. Repeatable replay requires unchanged inputs; use graph/input provenance or an archived
snapshot to prove file identity. For example, this records an explicit `--graph`
without reading it or creating reports:

```sh
DEBUNDLE_GRAPH='reports/tree/main/dependency_graph.json.gz' \
  debundle gate list --print-invocation
```

With minimal remote downloads, request `--remote_download_outputs=all` to bring
report side files local.

## Describe related IDs together

`describe --batch <file>` takes a nonempty JSON identifier array, such as
`["factory", "unit:12", "runtime/setup"]`, using ordinary `describe <id>` dispatch.
It preserves order and duplicates; a positional ID is mutually exclusive. Graph,
modules, source root, bounds and edge filters apply to every item. With `--tree-config`,
`--modules` may name the full app tree; IDs resolve against all roots for the graph's chunk.

One invocation loads/verifies the graph once and reuses binding-home lookups.
Module paths still resolve claims through the ordinary source-backed resolver.
Keep inputs unchanged; no analysis is cached between commands.

JSON wraps ordinary standalone reports in `{"results": [...]}`; NDJSON emits one
report per ID, and text prints in order. **All items must succeed before any result report**,
even NDJSON: failure exits nonzero with the ordinary structured error, item position
and ID, without partial results.

Invocation records retain the batch path and resolved graph/root options, not the
batch contents or fingerprint. Preserve the file alongside the worker input snapshot.

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

Report rendering, serialization, stdout and stderr writes, and explicit final
flushes are fallible. A successful operation whose stdout consumer closes the
pipe exits successfully. A stderr failure exits nonzero, even when stdout also
closes. If the operation failed, failure to emit its diagnostic on either
stream preserves that failure and the output error and exits nonzero. Text
reports finish rendering before writing any bytes.

An output error can happen after a spec edit was written. The CLI leaves that
edit applied and does not retry it; inspect the files before deciding whether
to run another mutation.

### Runtime failures from top-level queries

After Clap has accepted the command line, `describe`, `inspect-source`,
`graph-summary`, `atoms`, `coverage`, `scc`, and `cluster` keep runtime errors
machine-readable in JSON modes. A `json` failure writes one object with a
typed `reason` code and `message`; `ndjson` writes that same object as one row.
The message contains the full actionable error chain, and the command exits
nonzero while retaining the same diagnostic on stderr. Current reason codes are
`io`, `invalid_json`, `source_unit_not_found`,
`unsupported_identifier_prefix`, and `query_failed`. Unsupported-prefix
failures also include the identifier, prefix, accepted forms, examples, and a
suggestion when one is available.

Graph compatibility failures retain the existing `graph_compatibility`
rejection object, including `mismatch.reason` and regeneration details. With
`--format text`, runtime failures keep the existing stderr diagnostic and do
not write an error report to stdout. Clap usage and argument-parsing failures
remain Clap diagnostics because dispatch has not begun.

Read-only inspection commands provide graph, source-unit, atomic-group and
module-ownership evidence. `modules propose` searches the unseeded residual
domain for an exact placement; see [exact residual placement](module_proposals.md).

The mutating verbs (`bindings assign`, `bindings unassign`,
`bindings rename`, `modules apply`, `modules merge`, `modules delete`) take the same
`--format` flag with the same tty/pipe default. Under `text` they print a
one-line verdict (`<action>` plus move/file counts). Under a JSON format
each verb prints **one outcome object** sharing a common schema core:

| Field                   | Values                                                                                                                                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verb`                  | `assign` \| `unassign` \| `rename` \| `apply` \| `merge` \| `delete`                                                                                                                           |
| `action`                | `applied` \| `dry-run` \| `noop` \| `unchanged` \| `rejected`                                                                                                                                  |
| `gate`                  | `passed` \| `claims_checked` \| `names_only` \| `skipped` (`--no-verify`) \| `not_required` (edit cannot change the assignment); success outcomes only; see "Ownership-edit assessment states" |
| `needs_full_assessment` | `true` when an ownership edit passed only the graph-backed checks (no `--analysis`); omitted otherwise                                                                                         |
| `files_written`         | Files written (or, under `--dry-run`, that would be written)                                                                                                                                   |
| `files_deleted`         | Files deleted (or, under `--dry-run`, that would be deleted)                                                                                                                                   |

Verb-specific fields flatten in alongside the core: `moves_applied`
(assign), `unassigned` (unassign), `identity` / `old_readable` /
`new_readable` (rename), `source_units_reassigned` /
`residual_source_units_claimed` / `synthesized_claims` (apply), `target` (single
merge) / `targets` (merge batch).
When requested, `bindings assign`, `bindings unassign`, `modules apply`,
`modules merge`, and `modules delete` also return
`effect_relocation_assessment`, a report-only assessment of their exact
in-memory candidate plan. Most commands require `--dry-run --analysis <artifact>`;
`bindings assign` and `modules apply` also accept `--analysis` when writing,
assessing the final candidate before changing YAML. This requires `--graph`
and the full tree transform context (`--tree-config`,
`--tree-modules`, `--tree-vendor-marks`, `--tree-source-root`, and
`--out-root`); `--modules` and `--tree-modules` must resolve to the same root.
`modules apply` reuses its existing `--modules` root as `--tree-modules` and
its existing `--tree-config`, avoiding a second root/config argument. It
requires the auxiliary tree inputs to be explicit; it does not inherit
`DEBUNDLE_TREE_SOURCE_ROOT` or `DEBUNDLE_OUT`.
Other existing assessment commands retain their transform environment defaults.
The current path supports one full-tree graph chunk. It loads complete typed
facts through the canonical pipeline and verifies source, engine and analysis
projection identities; diagnostic reports are never decoded as facts. The
effect commutation result remains report-only; the legacy edit gate still decides
ordering and rebinding. For writes with `--analysis`, the canonical placement
projection must yield emitter maps, and a refusal leaves YAML untouched.
Omitting `--analysis` preserves the ordinary graph-backed edit behavior;
`bindings assign` and `modules apply` then report
`effect_assessment_unavailable: analysis_not_supplied`. Gate rejections replace the
success object with a structured rejection object — see "Rejection
diagnostics" below.

For `modules delete`, every requested file must resolve to a module in the
complete typed module tree. The exact resolved deletion set is used by both the
legacy edit gate and the assessment candidate; non-empty files still require
`--force`. Empty owners also occur in the emitted universe and entry fanout:
their deletion requires the gate and removes their priority slots. The
assessment reports the emitted order after those module
documents are removed. As with the other edit assessments, it proves only
statement-order commutation for that candidate and does not replace the gate.

## Whole-module merge batches

Use a batch for several merges or filename replacements with one validation of
the complete prospective spec. For example, `merges.json`:

```json
[
  { "target": "models/Alpha.yaml", "sources": ["parts/first.yaml"] },
  { "target": "models/Beta.yaml", "sources": ["parts/second.yaml"] }
]
```

```bash
debundle modules merge --batch merges.json --modules "$MODULES" \
  --graph "$GRAPH" --source-root "$SOURCE_ROOT" --dry-run
# Apply the same validated plan by omitting --dry-run.
```

Explicit `.yaml` filenames preserve their physical casing. Each entry uses the
single-merge claim, annotation, comment and note rules. The batch rejects empty
entries, reused sources, source-target chains, paths outside the module root and
case-equivalent logical destination collisions, including untouched modules.
Resolved files must end in `.yaml`; module trees containing symlink files or
directories are unsupported and rejected before planning writes.
Case-only relocation within one entry is supported. `--batch` cannot be combined
with `--target`, positional sources or `--no-verify`. A rejected plan or gate writes nothing;
after success, all targets are written before any source is deleted. Individual
file replacements are atomic; the batch is not a crash-atomic transaction.

## Name and identifier resolution

Binding names are resolved in the command's input view:

| Commands                                                                      | Accepted binding names                                                                                                                    |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Graph queries: `describe`, `inspect-source --id`, `cluster`, `scc --binding`  | The saved graph's input binding or exported/readable name.                                                                                |
| Spec edits: `bindings rename`, `assign`, `comment`                            | A direct selector identifier, source-match capture local, or explicit readable name; structural members require a readable name.          |
| `bindings unassign`                                                           | A direct selector identifier or explicit readable name; structural members require a readable name. Source-match entries are unsupported. |
| Raw source lookup: `inspect-source --around-binding` / `--declarator-binding` | The original input declaration's name, without spec-name resolution.                                                                      |

These names can differ. A source-match template may call a declaration
`templateToken`, match the input binding `raw`, and export `readableValue`.
Graph queries accept `raw` or `readableValue`; spec operations accept
`templateToken` or `readableValue`. The template-local token identifies a
capture in the selector template; its spelling alone does not say whether it is
stable or volatile, and it is not inherently an input-bundle name. After an
edit, a saved graph still reflects its pre-edit names until regeneration.

An imported name belongs to the provider's export namespace. Trace the
importer's local binding through its import slot, any re-exports, and the
provider's original export declaration before inspecting the provider's raw
binding. For example, `export { factory as build }` makes an imported `build`
refer to `factory`, even if the provider also declares an unrelated local
`build`. `inspect-source --around-binding build` would inspect that unrelated
local. Preserve the complete identity chain when auditing a callee's effects.

Use `bindings list --modules <modules-dir>` to discover current editable
identities; `--in <module-path>` limits the owner. JSON rows carry a typed
`identity` and, when explicitly configured, a separate `readable_name`. Text
rows label each identity kind; `unrenamed` means no explicit readable name:

- `direct_binding_identifier` has `name` from `selector.binding.name`.
- `source_match_capture` has `local` from `source_matches[].bindings[].local`.
- `structural_member` has `selector` from the typed member selector, such as
  `cross_ref`; it has no direct binding identifier. An invalid
  selector is reported as `members.selector`.

Neither a direct identifier's spelling nor a capture's local spelling alone
implies volatility. A source-match capture can use `Widget` in its template
while matching the input binding `raw`; use `describe <readable-name> --graph
<matching-graph> --modules <modules-dir>` and read `source_unit_ids` and
`bindings[].binding`, or inspect it with `inspect-source --id <readable-name>`
and the same graph/spec context, to recover the input identity. `bindings list`
needs no saved graph. An ambiguous spec identity is rejected with matching
file/claim locations; choose an unambiguous identity from that inventory.

Graph queries resolve module paths and unambiguous module basenames before
binding names. Check `query.kind` before interpreting a name lookup as a
binding lookup. If a readable name collides with a module basename, describe
the binding's current full home-module path and find the row with the intended
`bindings[].export_name`; inspect the report's truncation metadata before
using its `binding` value. The first binding in a module report need not be
the requested readable name.

For bulk name recovery of module claims, use the matching per-chunk `modules.json` report:
each `final_module_contents` row has positionally aligned `binding_names`
(input names) and `member_names` (emitted names), sorted together by input
name. Pair these arrays within the row's `path` instead of describing every
binding separately. Use the row's sorted `binding_source_units` entries to map
each `binding_name` to its declaration `source_unit_ids`. These IDs include
redeclaration sites, do not include units that only read/write/call the binding,
and use the same chunk-local `unit:N` identities as the graph report. The
module-level `source_unit_ids` remains an aggregate and is not positionally
aligned with the binding arrays. Use the report for the same source/spec/engine
snapshot; it does not resolve later spec edits.

`describe` and `inspect-source --id` dispatch bindings (minified or readable),
module paths (`runtime/plugins`) or unambiguous module filenames, module IDs
(`logical:7`), source-unit IDs (`unit:42`) and atom IDs (`atomic:N`). An
unknown identifier-like prefix such as `module:7` is rejected before graph
loading or compatibility checks, with `logical:7` suggested for a module ID.
An existing module path containing a colon is resolved as a path before this
check.

`describe` emits at most 20 rows in each report section by default. Use
`--limit N` for a different positive per-section cap, or `--all` to return
every matching row; `--all` conflicts with `--limit`, and `--limit 0` directs
you to use `--all`. Its `limits.sections` object records `total`, `emitted`,
and `truncated` after filtering; `limits.limit` is the cap, or `0` with
`--all`. The selected binding/source-unit/group row comes first in its matching
section so it remains available under a small limit.

`--direction incoming|outgoing|both` selects edge direction relative to the
query and defaults to `both`. It filters dependency, atomic-group, and
module-quotient edges; neighbor source units are derived from the remaining
dependency edges. Repeat `--reason KIND` to retain any of several dependency
reasons, using the `edge_kind` spellings from `dependency_graph.json.gz`:
`eager_use`, `lazy_use`, `hoisted_use`, `eager_rebind`, `lazy_rebind`,
`deferred_rebind`, `sequenced`, `co_declaration`, `co_location`, or
`local_effect`. Both
filters are applied before totals and row limits.

`cluster <binding>` also emits at most 20 incoming and 20 outgoing module
neighbors by default. `--limit N` sets the positive cap for each direction;
`--all` emits every neighbor and reports `limits.limit: 0`. Its
`limits.sections.incoming_modules` and `outgoing_modules` entries report
`total`, `emitted`, and `truncated` after `--direction` and repeated `--reason`
filters.

## Validate-by-default (mutating commands)

Every command that modifies the spec runs validation **by default** before
writing changes back to disk. For commands that affect the chunk's module
assignment or emitted owner universe, that means
the post-edit claim checks followed by the full realizability + atom-split
gate — which requires `--graph <dependency_graph.json.gz>` unless `--no-verify` is set.
Direct local claims must identify graph source units, typed `kind` pins must match
one source chunk (a repeated declaration may satisfy the kind at any site),
and `import_specifier` pins must name an import specifier. Pass `--source-root` when
the graph's relative source paths are not otherwise resolvable. Renames run
name-collision detection; comment edits run shape preservation only (they take
`--dry-run` but no `--no-verify`). If validation fails the command refuses
with a structured diagnostic and **does not modify any file**.

For these edit commands, a successful JSON result means the post-edit
source-claim, assignment, and realizability checks passed. Neither
`claims_checked` nor `passed` means the full reconstruction was accepted:
`debundle run` remains the
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

### Ownership-edit assessment states

The ownership edits (`bindings assign`, `bindings unassign`, `modules apply`,
`modules merge`, `modules delete`) report these
validated states:

| `gate`           | `needs_full_assessment` | Meaning                                                                                                                                             |
| ---------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claims_checked` | `true`                  | Graph-only: the post-edit source-claim, assignment, and legacy realizability + atom-split checks passed; no full candidate assessment ran.          |
| `claims_checked` | omitted                 | `--analysis` assessed the full candidate (a dry run), but its placement is not mappable for canonical emission; see `effect_relocation_assessment`. |
| `passed`         | omitted                 | The same graph checks passed, and the full candidate assessment (`--analysis`) found the candidate placement mappable for canonical emission.       |

Graph-only writes are still permitted: the legacy edit gate remains the
authority that vetoes an edit, and `--analysis` stays opt-in. A graph-only
write (not `--dry-run`) prints one stderr line warning that it was not
assessed against the full candidate.

## Running the pipeline

```bash
debundle run --spec <transform-spec.yaml>

debundle run \
  --tree-config <spec-config.yaml> \
  --tree-modules <modules-dir> \
  --tree-vendor-marks <vendor-marks.yaml> \
  --out-root <out-dir>
```

If the gate rejects an assignment, `debundle run` emits each nonempty
structured evidence report (`cycles.json`, `atomic_group_conflicts.json`) under
`reports/tree/<chunk-id>/`. Both can be present when a split atomic group also
forms a blocking SCC; the atomic conflict remains the primary verdict and
diagnostic. `graph_summary.json` records both counts. A missing `cycles.json`
means zero blocking SCCs only for a complete, current report set. `debundle run --dry-run` runs pipeline
parse/facts/gate checks without writing emitted JS or reports; a gate
rejection still writes its rejection evidence. Full graph serialization is
opt-in with `--graphs`, including for rejected runs. Graph-backed queries require
that export; the default `graph_summary.json` has exact source-unit/edge/kind/atom
counts and gate/legacy-TV verdicts, with no node/edge table. All internal analysis,
gate and translation-validation checks run in either mode.

`debundle run --analysis <artifact> --reports-only <out-root> [--graphs]`
runs the same canonical materialization and final program validation, writing
only reports under `<out-root>/reports/tree`. It writes no JS, browser or vendor
assets and requires a precomputed analysis artifact. The reports-only output
mode is independent of graph and effect-stream selection. It conflicts with
`--dry-run`; an explicit `--out-root` must name the same root. With
`--emit-on-rejection`, a gate-only rejection retains typed exit 3 and writes a
truthful `GATE_REJECTED` marker at the reports-only root. Other errors remain
failures, and accepted exports exit 0. Existing output directories must be empty.

`debundle run --emit-on-rejection` (exclusive with `--dry-run`) additionally
writes the logical-module tree of a gate-rejected spec for reading, naming and
architecture review: the run still exits nonzero with the same diagnostic and
the same reports, but the `write_js_tree` directory (else the browser harness
directory) gets the entry and module files plus a `GATE_REJECTED` marker at its root naming the evidence report, and
each rejected chunk's entry opens with a banner comment saying the same. No
browser harness or vendor assets are written. Exit status 3 means the gate's
rejection was the only failure and the tree is complete; any other failure
exits 1. The contract is
<../SPEC.md> § Reading-only output on gate rejection; the tree is not runnable.

`translation_validation.json` keeps exact gate/TV verdicts and category totals,
with at most twenty deterministic examples per category. Each evidence field
has `total`, `examples` and `truncated`; use totals for comparisons.

### Residual atomic group report

To inspect the atomic units that remain under the current assignment, including
an ownership subset, request `debundle run --full-report residual_atomic_groups`.
The per-chunk `residual_atomic_groups.json` lists every group containing a
residual source unit. Each source unit has its source location, statement kind,
bindings (empty for anonymous statements), and actual destination. Groups also
include incoming and outgoing edge counts by kind and up to four examples per
kind and direction, chosen from distinct neighboring groups; `truncated` marks
incomplete examples. Edge totals count
graph edges, not distinct dependencies. `fully_residual: false` means a group
spans residual and selected destinations; the gate verdict remains authoritative.
This report works with `--ownership-subset` without `--graphs`; it is a planning
aid, not proof that extracting a group or batch passes the gate. Select chunks
with `--full-report-chunk` or Bazel `full_report_chunks`, and request it through
Bazel with `full_reports = ["residual_atomic_groups"]`.
Entry import and export declaration units may appear in this inventory as
scaffolding; their presence does not suggest moving them out of the entry.

### Structural atoms and extraction candidates

An atomic group is the set of source units that the current graph and analyzer
contract require to share a destination. Separate groups are not necessarily independent
extraction candidates: directed ordering edges between them can become a cycle
after the current assignment is projected onto modules. The
`residual_atomic_groups` inventory describes those groups and their edges for
the run's actual assignment. The inventory describes the current run assignment
(including an ownership subset, if selected); its `gate_accepts` value is the
shared-gate verdict for that assignment. It says nothing about a different
hypothetical extraction. Run the shared gate on the complete proposed
destination assignment.

For example, suppose three distinct structural atoms have required execution
order `A` then `B` then `C`. Extracting only `B` while leaving `A` and `C` in one
residual destination can project the order constraints like this:

```text
execution order in the source:  A → B → C
destinations after the peel:    residual(A,C) → B → residual(A,C)
```

The arrows here show execution order, not dependency-edge direction. The
residual destination appears on both sides of `B`, so the complete assignment
may have a mutual constraining cycle and fail the gate even though no structural
atom was split. Splitting the residual across destinations or extracting a
coherent batch can change that quotient and may pass; each resulting assignment
still needs its own gate verdict. Passing one ownership subset does not imply
that adding or removing claims will pass, because that changes the destinations
and their quotient. See [the design distinction](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#two-classes-of-atom)
for structural atoms, conservative effects, and the accepted lazy-cycle case.

### Residual placement

Use [`modules propose`](module_proposals.md) to construct a peel in its documented
append-one-owner closure family. For a specific single-atom or joint move,
provide the complete source-unit assignment and final import priority to
`modules check --batch`; it checks the full assignment, including every atom
and ESM evaluation. Apply only a checked plan or batch with `modules apply`,
then run normal materialization and relevant runtime checks. A completed
proposal search covers only its documented family, not arbitrary partitions
or moves into existing owners.

`residual_atomic_groups` remains an inventory of groups in the actual current
assignment. Use `atoms --residual-only`, `describe`, and `inspect-source` to
inspect their units, ownership and source spans. These inspection commands do
not validate a hypothetical move.

### Other full reports

`debundle run --full-report export_reentry` writes the original complete
export-reentry schema as compact `export_reentry.full.json`. The default
`export_reentry.json` has exact collection and site counts, at most twenty
deterministic examples per collection, and explicit truncation. The complete
semantic report and staged payload still drive policy and checks. Full-file
serialization and explicit flush finish before the summary; failures fail the
run. Use `--full-report-chunk` to select sinks, independently of graph export.
Bazel `full_reports = ["export_reentry"]` forwards the same request; full
report kinds can be requested together. See the
[wire contract](wire_format.md#export-reentry-diagnostics).

`debundle run --full-report translation_validation` writes complete checker rows
in `translation_validation.full.jsonl`. Repeat `--full-report-chunk <chunk-id>`
to select only those sinks; configured analysis, summaries, checks and verdicts
stay unchanged. The summary advertises this command even when no full stream
was requested. Full diagnostic evidence and full graphs are separate requests.
An accepted dry run refuses an explicit full-report request because it suppresses
reports. Rejected dry runs retain their normal evidence policy. Invalid specs,
unknown chunk filters and write/flush failures remain errors; complete streams
flush before the final summary is published. Bazel `full_reports =
["translation_validation"]` and optional `full_report_chunks` forward these
flags only to Pipeline, leaving Analyze cache inputs unchanged.

`debundle run --effect-conflicts-jsonl` opts into a complete per-chunk
`effect_conflicts.jsonl` beside the bounded effect-TV summary. It uses the
same typed facts and emitted order, streaming every conflicting pair and
cell with access provenance, premise disposition and endpoint opacity.
Repeat `--effect-conflicts-chunk <chunk-id>` to restrict JSONL sinks, for
example `--effect-conflicts-chunk mcp --effect-conflicts-chunk remoteWorkerWire`.
It requires `--effect-conflicts-jsonl` and rejects names outside the configured
materialized chunks. It changes only JSONL emission: the full spec, loaded
sources, analysis/census scope, graphs and bounded summaries stay the same.
Default output remains bounded. The flag requires a configured report
directory; rejected dry runs can export their normal rejection reports,
whereas accepted dry runs refuse this request because they suppress reports.
It also works with `--analysis` and `--emit-on-rejection`. Write or flush
errors fail the run; verify row/cell counts against the sibling summary,
which is written after the stream flushes. A gate rejection can still have
a complete export while the command exits nonzero. The gate and
coverage assumptions are unchanged. Format:
<wire_format.md#translation-validation>.

### Ownership subsets with full selector context

Use `run --ownership-subset retained.json` for ownership-only reduction and
restoration. Keep the complete canonical module tree and its contracts:

```json
{ "modules": [{ "chunk": "app", "module": "features/view" }] }
```

Each entry names an exact chunk ID and a chunk-local module path, as recorded
in `modules.json`'s `requested_logical_modules[].target_path`. Paths are not
physical YAML filenames or chunk-prefixed IDs. `{"modules": []}` retains no
named modules. Unknown chunks/modules, duplicates and unknown fields fail.
Add an optional `bindings` include list to retain only named claims from a
selected module, using the module's authored readable member/export names:

```json
{ "modules": [{ "chunk": "app", "module": "features/view", "bindings": ["parseView"] }] }
```

Omitting `bindings` retains all named claims in that module; an empty list
retains none. Add an independent `anonymous_statements` include list to select
zero-based indices in the module's authored `anonymous_statements` selector
list:

```json
{ "modules": [{ "chunk": "app", "module": "features/view", "bindings": [], "anonymous_statements": [1] }] }
```

Omitting `anonymous_statements` retains every anonymous claim; `[]` retains
none. Thus `bindings: []` with no anonymous field retains only the module's
anonymous claims, while `anonymous_statements: []` with no `bindings` field
retains all named claims and no anonymous claims. A retained whole statement
also retains its declarations; a filtered-out statement and declarations
adopted only from it return to ordinary unassigned routing. Unknown or repeated
names, and out-of-range or repeated selector indices fail. The option selects
ownership claims; ordinary source-unit placement and the gate still decide
whether a partial selection is realizable.

To add a fresh owner while retaining the same base tree, put ordinary selector
claims in the view's `placements` map:

```json
{
  "modules": [{ "chunk": "app", "module": "features/view" }],
  "placements": {
    "app": {
      "features/parser": {
        "source_matches": [
          {
            "match": "function parse(input) { return JSON.parse(input); }",
            "bindings": [{ "local": "parse", "name": "parseInput" }]
          }
        ]
      }
    }
  }
}
```

This requires `inline_in_entry` and an explicit final priority containing the
retained and fresh owners. Fresh paths cannot match an existing base path,
including an omitted owner's path. Keep semantic premises in the full base;
placement modules cannot add annotations or anonymous callback premises.
Placement selectors may refer to exports of omitted base modules and claim
code those omitted modules used to own. Retained selectors keep the full
solve's binding decisions; removing competitors never reruns their identity
choice. Retained whole-statement and destructuring companions remain occupied.
The normal gate checks the resulting complete assignment and supplied order.
Graph-backed editing of selected views remains a separate writer capability;
manual placement materialization alone does not make a proposal executable.

Build allowlists from the authored member names: the `readable_name` in
`bindings list` or the member's effective `name:`/default export name. In
`modules.json`, `final_module_contents[].member_names` shows emitted names and
`binding_names` shows input binding spellings; do not use `binding_names` as
allowlist names. Final module rows can also contain bindings pulled in by
whole-statement placement, so check a candidate name against the authored
claims before counting it. When combining source-unit rows from multiple
chunk reports, identify a unit by both `chunk_id` and its `unit:N` ID.

```sh
debundle run <full tree args> --analysis analysis.dba \
  --ownership-subset retained.json
```

The full joint selector solve and all analysis assertions remain unchanged.
The retained ownership and any fresh placement claims are combined before
normal residual routing.
In `coarse_atomic_groups`, `complete_partial_atomic_groups_for` may list
authored module paths that can adopt unclaimed members of atoms they solely
own. The paths are validated against the full authored module roster; a listed
owner removed by the subset is not recreated and cannot adopt source units.
Selected module names and explicit entry renames stay intact. Omitted members
keep their source names in entry with `inline_in_entry`, in the configured
catchall with `catchall_file`, in one module per atomic group
with `per_atomic_group`, or in generated residual groups with
`coarse_atomic_groups`. Coarse mode first applies any listed sole-owner
completions, then merges remaining wholly unclaimed atomic groups
with equal reachable fixed-owner sets in the strict constraining projection
within intervals between fixed owners in the fine layout's simulated
evaluation order, provided the merged module keeps each public export name
associated with one source binding or export site. Incompatible groups remain
separate with their names intact. Simulator-omitted groups remain separate.
Their paths use
`__auto/residual_groups/NNNN`. Authored names remain in the
complete spec. Invalid full-spec selectors or duplicate/unmatched claims still
fail even when their modules are omitted.

Normal gates, translation validation, reconstruction and emission run on the
selected assignment. `graph_summary.json` and `modules.json` carry its
`ownership_subset` module paths and optional binding and anonymous-selector
index lists, preserving omitted fields versus empty selections. This is partial
output; it does not validate the full canonical assignment. Full graph export
is available with `--graphs`; the dependency graph records the normalized
selection and its program provenance fingerprints the exact subset-file bytes.
For a passing `inline_in_entry` selected baseline, `modules propose/check/apply`
can append one fresh residual owner. The writer stores ordinary selector claims
in the subset JSON's `placements` map and the checked final priority in the
same tree configuration. It retains the full base tree and its semantic
premises, retained bindings and existing placements. Destination paths for this
selected-view workflow are chunk-local owner keys in proposals, explicit batches
and plans. It preflights the actual
staged view through the normal materializer and compares the complete ownership
assignment with the graph-checked candidate before creating an executable plan
or writing. A selector failure returns no plan. Other selected-view binding and
module edits remain unsupported, as does optional `--analysis` effect assessment.
Use the same `--tree-config`, `--modules` and `--source-root` context for the
workflow in <module_proposals.md>. Do not treat selected graph unit IDs as durable
spec coordinates. An omitted explicit catchall claim can be recreated as ordinary
residual ownership without its authored renames. Use the same full projection
and matching analysis artifact across selection edits; an engine, source or
analysis-contract change still requires a matching fresh analysis.

Bazel's `debundle_apply` accepts `ownership_subset = "retained.json"`.
Keep `tree_modules` and its declared inputs complete. Pipeline and split graph
export actions both declare and read this manifest; changing it leaves the
projection and Analyze inputs unchanged. A graph-backed `debundle_cli` runner
for that output must pass the same label through its `ownership_subset` argument
so the launcher relocates the captured subset root.

### Reusing analysis across runs

Analysis dominates a run, and module assignment edits do not change it. Compute
it once and reuse it while editing the assignment:

```bash
debundle spec project-analysis <run args> --out analysis.projection
debundle analyze --projection analysis.projection \
  --tree-source-root <root> [--package-root …] --out analysis.dba
debundle run <run args> --analysis analysis.dba
```

The projection holds everything analysis reads from the spec; its bytes stay
the same across assignment edits, module renames, YAML-only author notes and
graph-only `commuting_statements`. Only fields declared as author notes are
omitted. Semantic map entries remain data even when a chunk, export, or property
name is literally `note`; commuting-contract notes stay with the graph report.
`run --analysis` refuses an artifact computed by another engine, from other
sources, or for another projection, naming what differs; rerun the first two
commands then. Its output equals `debundle run`'s. Under Bazel,
`debundle_analysis` owns projection and Analyze, while each `debundle_apply`
requires that shared target and owns the final action. An assignment edit reuses
the cached `DebundleAnalyze` result. The `analysis` output group holds the
projection and artifact for running the last command locally.

After a graph-only build, the analysis artifact may still be in the remote
cache rather than the local output directory. Request its output group before
assuming analysis must run again:

```bash
bazelisk build //path/to/spec:debundle --output_groups=analysis
```

This requests `<target>.analysis_projection` and `<target>.analysis`. With the
same projection, sources and analysis engine, Bazel can retrieve the previous
analysis action's outputs. Check the actual cache/execution result; a missing
local file alone is not evidence of a cache miss.

For a copied assignment experiment, use the matching analysis with fresh spec
resolution. Saved-graph editing commands check their recorded YAML membership
and contents; manually editing that YAML first makes the saved graph stale.
Do not bypass that check. To collect reports across gate-rejected chunks
without writing their JS trees, use:

```bash
debundle run <run args> --analysis analysis.dba \
  --reports-only experiment-reports --emit-on-rejection
```

Add `--graphs` only when the next investigation needs saved-graph queries;
summary and cycle reports do not require full graph serialization. Exit 3 means
only gate rejection and complete inspection output; exit 1 may leave a partial
report set. `--dry-run` or `--reports-only` alone does not
establish a complete inventory after a gate failure. The first console error
can concern another chunk, so inspect the selected chunk's native report and
check report-set completeness separately.

Selector failures follow the keep-going (default) and `--fail-fast` modes of
<../SPEC.md> § Modes, in the order of `docs/selector_resolution.md` § Order. Bulk
selector edits keep going, reporting every failure of a pass in each chunk's
`selector_diagnostics.json`; use `--fail-fast` only when the first failing
selector or claim is the useful debugging target (its outcome line is the
error).

Every chunk's selectors resolve as one program. In a tree spec, several
module trees may scope to one chunk. Their entities never claim the same place.
See [tree spec configuration](spec_editing.md#tree-spec-configuration).

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

To list annotations the engine may already infer, run `debundle spec lint`
with the same inputs as `spec validate`. It costs one validate pass, whatever
the number of annotations: each finding's signal comes from that pass. JSON
gives `findings[]` (typed per [SPEC](../SPEC.md#spec-lint)) and
`unlinted_chunks`, plus `pass_error` when the pass failed after linting
(text prints it as a `note:` line); text prints one `warning[inferable_annotation]:` line per
`inferable` finding and a `note[...]` line per `inconclusive` one, naming the
module/member or spec location. It exits 0 whenever the pass produced reports.
`host_behavior.reentry_free_operations` entries are decided from recorded uses
at no extra cost (<premise_provenance.md>). `--probe-analysis` additionally
decides `purity: pure` hints and consumed operation entries by rerunning the
whole program once per annotation with that annotation withheld
([SPEC](../SPEC.md#spec-lint)); `--probe-chunk <chunk>` and `--max-probes <n>`
bound it, and `probes[]` gives each run's outcome and `millis`. Budget it as one
`run --dry-run` per hint.

To find authored chunk-export contracts that this exact assignment does not
currently need, run a bounded fresh audit:

For standalone audits of large captured programs, first prepare the
[optimized CLI and adapter inputs](bazel_integration.md#standalone-cli-on-captured-bundles).

```bash
debundle spec audit-contracts --tree-config spec_config.yaml \
  --tree-modules modules --tree-vendor-marks vendor_marks.yaml \
  --out-root out --tree-source-root source \
  --policy-key static/peer --max-probes 20 --format json
```

The command accepts the same flat `--spec` or tree inputs and package roots as
`run`. Use `--kind`, `--export`, `--member`, and a typed JSON `--signature`
(`{"argument_count":1,"callback_arguments":[0]}`) to narrow the target set.
It starts with a passing full-pipeline baseline, then removes one target at a
time in memory and recomputes full acceptance. Within one invocation it reuses
parsed source, preparation indexes, and read-only vendor resolution; each probe
gets its own mutable AST and recomputes contract-dependent analysis, assignment,
lowering, and final reconstruction checks. The captured inputs are reverified
before and after probes; input changes abort the audit. See the
[measurements and profiling recipe](../perf/contract_audit.md). Results beyond
`--max-probes` are explicitly `budget_skipped`. `removable_under_remaining_assumptions` is
conditional on every other authored contract and runtime premise, for this
source and assignment only; it neither proves independent purity nor permits
joint deletion of all passing targets. Failed probes report the acceptance
error, and a setup failure is inconclusive. No removal is applied. Source
marker keys are reported as policy labels. The probe produces no graph or
output artifacts, since a graph for the edited in-memory spec could not
honestly carry the original YAML's provenance.

Progress goes to stderr. It reports baseline start and completion, then start
and completion for each attempted probe. Probe events include the typed target,
its 1-based ordinal among probes allowed by `--max-probes`, the eligible count,
the number completed, and the verdict. Budget-skipped targets remain in the
aggregate report without probe events. Text, JSON, and NDJSON stdout remain one
complete aggregate report; a baseline or probe failure before report completion
leaves stdout without a partial report.

Add `--checkpoint audit.json` to create a new checkpoint, or replace it with
`--resume-from audit.json` to resume one. The modes are mutually exclusive;
creation refuses an existing file. Resume reruns full baseline acceptance and
requires the same complete input provenance, engine, effective pipeline options,
roots and normalized target selection before reusing definitive successes or
acceptance failures. Inconclusive work is retried. Each probe still removes only
one contract from the original spec.

`--max-probes` caps the total deterministic target prefix, including reused
results. Raise it to extend an audit. Lowering it narrows the report while
retaining later checkpoint records. Reused targets emit `audit-progress probe reused`
instead of the normal probe start/completion pair. Stdout remains the complete
report. Budget skips are derived and never cached.

The checkpoint directory must exist. A kernel lock on `<checkpoint>.lock`
refuses concurrent writers; leave that sidecar in place. Each completed,
input-verified definitive probe replaces the checkpoint atomically through a
sibling temporary file. A failed baseline, identity check or replacement leaves
the previous complete file intact. This recovers completed work after process
interruption; it does not promise power-loss durability. Checkpoints contain
source/spec identity, target names and probe errors: store them with the input
artifacts.

Every `spec validate` format includes a `validation` summary. JSON places it
beside `counts` and `outcomes`; the final NDJSON `summary` line includes it;
text prints `Selector validation: success|failed; checked=N, matched=M`.
`checked` counts each evaluated member, expanded
`source_matches[].bindings[]` entry, and anonymous statement. Invalid or
unmatched selectors count as checked but not matched. `matched` counts entities
whose final selector outcome is resolved, including warning outcomes. The
summary's `success` means all checked selectors matched and there are no
error-severity selector outcomes. Module-level diagnostics such as stale
annotations can fail `success` without increasing `checked`. Issue rows are
diagnostics, not the source of the summary counts. A zero/zero summary is
successful but explicitly means no selectors were evaluated. This status covers
selector validation only, not gate acceptance, emission, or runtime behavior;
`--report-only` changes only the exit status for selector errors. If the full
pipeline cannot produce reports for all configured chunks, validation returns
the pipeline error rather than publishing partial counts.

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
debundle inspect-source --source-file app.js --declarator-binding alias --view raw
debundle inspect-source --id atomic:17 --graph dependency_graph.json.gz --modules spec/modules \
  --source-root extracted
debundle inspect-source --id runtime/plugins --view raw --context-statements 1
```

`--statements` accepts one zero-based index or an inclusive `START..END`
range. With no selector it starts at the first item and prints up to the
configured statement limit. `--around-binding NAME` locates
the unique top-level item that declares that raw input name and includes two neighboring
items on each side by default; set `--context-statements` to change that count.
For one variable in a large comma-separated declaration, use
`--declarator-binding NAME`. It selects that unique variable declarator without
rendering siblings or surrounding items. Its `index` still names the enclosing
raw item; `declarator_index` names the selected fragment, whose coordinates and
raw source are exact. Pretty view renders it as a standalone declaration.
The isolated declarator view omits the enclosing `var`/`let`/`const` kind;
inspect its returned raw `body[N]` index before claiming that kind for a
source proof such as a primitive Set operand alias.
This selector cannot be combined with `--context-statements`.
For a readable spec name, use `--id NAME` with a matching graph and modules;
see [name and identifier resolution](#name-and-identifier-resolution).
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
A long single-line import can be clipped by the 512-byte line limit; set
`--max-line-bytes 0` while selecting only the needed statement(s), and keep
`--max-output-bytes` finite. If the total output cap omits part of that bounded
selection, raise it deliberately.
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
the first place it diverges. Its `reason` is structured evidence with a `code`
and variant-specific fields, rather than rendered prose. For example,
`variable_declarator` carries `index`, `selector` and `candidate` syntax summaries;
`class_member_missing` carries the member key. Text output renders that same
evidence for people. After an upstream bump, the first entry is usually the code
the selector should now match:

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

## Names-only batches (`bindings rename`)

Rename one binding with the existing positional pair, or supply a JSON array
with `--batch <file|->` (`-` reads stdin). The modes are mutually exclusive:

```json
[
  { "sym": "Alpha", "readable": "Beta" },
  { "sym": "Beta", "readable": "Gamma" }
]
```

Each row requires `sym` and a nonblank `readable`; unknown fields are rejected.
Use one chunk's `--modules` root per invocation. Lookup and collisions cover
that entire recursive root, so an aggregate root cannot partition names by chunk.
No graph is required. This changes names and annotation keys without moving
claims or rewriting selectors, template locals, template bodies, or notes.

All rows resolve against one pre-edit snapshot. Identical requests for the same
resolved binding collapse; contradictory requests and ambiguous lookups reject
the complete batch before writes. Each requested target must have a unique final
owner among claimed member and source-match identities. Chains and swaps are
allowed; unrelated pre-existing duplicate names do not reject a fresh target.
`--no-verify` skips name collisions, as for the positional form.

Annotations move with their binding after all moving keys are removed. Ambiguous
annotation ownership and occupied or orphan target metadata reject the batch,
even with `--no-verify`. Other annotation entries remain untouched.

The aggregate outcome has `renames_applied` (changed bindings), canonical
`renames` rows (`sym`, `module`, `identity`, `old_readable`, `new_readable`),
and the shared mutation fields. `identity` uses the same typed shape as
`bindings list`; `old_readable` includes the effective fallback name.
A verified nonempty batch reports `gate: names_only`; an empty array reports
`action: noop`, `gate: not_required`. A requested current name reports
`unchanged` without rewriting shorthand/default encoding, after collision checks.
`--dry-run` runs the same preflight and reports every file that would change.
Each changed YAML file is replaced atomically; this is not a crash-atomic
transaction across files.

Aliases resolve anew on every invocation: after a chain or swap, replaying the
same readable names can select different bindings or become ambiguous. There is
no replay-idempotence promise. Names-only validation does not prove selector or
free-reference identities. Run the separate source-only validation and compare
its outcomes and templates; use fresh graph-backed validation for relational
selectors. See [Naming](naming.md).

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

To include the report-only effect assessment, add `--analysis <artifact>` and
the full tree transform context to a graph-backed dry run. For example,
`bindings unassign` accepts:

```bash
debundle bindings unassign XOe \
  --modules "$MODULES" --graph "$GRAPH" --source-root "$GRAPH_SOURCE_ROOT" \
  --tree-config "$TREE_CONFIG" --tree-modules "$MODULES" \
  --tree-vendor-marks "$VENDOR_MARKS" --tree-source-root "$SPEC_ROOT" \
  --out-root "$OUT_ROOT" --analysis analysis.dba --dry-run --format json
```

The same opt-in applies to `bindings assign`, `modules apply`, and
`modules merge`. For `modules apply`, the existing `--modules` root and
`--tree-config` arguments also supply the transform's tree modules and config;
the command does not accept duplicate root/config flags.

This assessment currently supports one materialized graph chunk whose
`--tree-config` assigns the full module tree to that chunk (no scoped
`module_roots` entry). `--tree-modules` must resolve to the same directory as
the editor's `--modules`. The transform context and staged artifact are checked
against the verified graph source and the artifact's engine, source digests and
completed projection before the candidate plan is assessed. A missing, stale
or mismatched input fails before YAML is written. The JSON
`effect_relocation_assessment` field contains the common candidate assessment:
its effect decision and exact statement order plus typed placement obligations
from the same assignment. Status distinguishes effect conflicts from
structural placement blockers, and `legacy_gate_ordering_veto` makes the
remaining semantic-order gate veto explicit. Unresolved typed structural
checks keep the overall result incomplete. This does not change the legacy edit
gate or establish vendor replacement, privacy or singleton guarantees. Without
`--analysis`, the normal graph-backed edit path remains unchanged and reports
`gate: claims_checked` with `needs_full_assessment: true` (see "Ownership-edit
assessment states").

### `bindings assign --batch` JSON format

A top-level JSON array of move objects (`sym` and `module` required;
omitting `readable` preserves the current readable name):

```json
[
  { "sym": "XOe", "module": "runtime/plugins", "readable": "PluginSettingsAccessor" },
  { "sym": "YOe", "module": "runtime/plugins" }
]
```

## Checking explicit destination batches (`modules check`)

For residual discovery, use [`modules propose`](module_proposals.md), including
`--graphs` for a configured corpus. Keep each returned chunk-local plan together.

`modules check --batch changes.json` checks an explicit source-unit assignment
against the accepted starting graph. Supply the same `--graph`, `--modules`,
`--source-root` and `--tree-config` used by the other graph commands.
The batch is a JSON array of `{ "unit_id": "unit:1", "module": "helpers/value" }`
rows; omitted units keep their current destinations. With `--tree-config`, use
destinations under the configured physical module roots. `--batch -` reads stdin.

The complete check covers structural atoms, cross-module rebinding and global
ESM evaluation, including remaining residual code. It supports unions of atoms,
existing-owner changes and anonymous statements. Import/export scaffolding stays
in place. Unknown or duplicate source units and invalid destinations are errors.
A rejected starting assignment can be repaired only when the complete requested
result passes the same gate. The command writes no spec files.

By default the command dry-runs the same source writer as apply. Supply
`--source-root` or `DEBUNDLE_SOURCE_ROOT`; changing priority also requires
`--tree-config` so the checked order can be persisted. JSON output has
`scope: executable_plan`, independent `graph_accepted`, the checked changes,
residual units extracted, existing units moved and the full validation report.
`accepted: true` and exit 0 require both graph and writer acceptance. A graph
rejection or writer failure emits `accepted: false`, `plan: null` and exits 1;
writer failure includes `selector_realization_error`. Invalid inputs and stale
identities fail before acceptance. Emission and runtime remain separate checks.

For graph inspection or independent graph oracles, use
`modules check --graph-only --batch changes.json`. This needs no source root and
reports `scope: graph_only`; `accepted` equals `graph_accepted` and `.plan` is
always null. `--graph-only` conflicts with `--plan`. Independently accepted
batches must be checked together before combining them. Neither mode discovers
peels or proves that none exists.

Successful default JSON output contains `.plan`. Save that object and use
`modules check --plan plan.json` or `modules apply --plan plan.json` to retain
the checked graph/program identity. Plans use logical destinations even with a
configured physical tree root. `--plan` and a raw batch are mutually exclusive.

## Applying explicit assignments (`modules apply --assignments`)

Pass the same JSON array checked by `modules check --batch` to
`modules apply --assignments changes.json`. Omitted units retain their owners.
The command supports new residual declarations, anonymous statements, existing
claims and several destinations in one batch. Use `--dry-run` first; preview and
write perform the same checks. Both require the current graph, modules tree and
source context. `--assignments -` reads stdin.

Residual declarations receive proven structural `source_matches`; anonymous
units receive proven `anonymous_statements` selectors. An ambiguous selector is
a realization failure even when the graph assignment passed. Existing selectors
and binding/statement metadata move intact. Module notes/comments remain with
their owner, and this mode keeps empty authored owners so its module order agrees
with the checked assignment. A JSON map supplied with `--readable-names FILE`
can name newly claimed bindings; omitted names retain their source spelling.

Before writing, the complete scoped spec resolves again and its authored-claim
map must equal the starting graph's claims overlaid with the requested logical
destination changes. Omitted authored claims remain; unclaimed units continue
through the configured residual policy, whose generated filenames may be
regenerated or renumbered. The common graph checker then validates the candidate
assignment. Failure leaves the spec unchanged. JSON reports new residual claims,
existing units moved, synthesized claims and changed files. Graph/selector
acceptance still requires normal materialization and relevant runtime checks.
Refresh the graph after applying an edit.

## Applying assignments in a multi-chunk tree

Pass the matching `--tree-config` to `modules propose`, `modules check` and
`modules apply`. The config maps each graph chunk ID to its authored module
roots. Check and apply accept logical destinations under those roots and scope
the complete selector and assignment checks to the selected graph chunk.
Single-chunk specs can omit the config when `--modules` already names that
chunk's tree.

For newly claimed bindings, `--readable-names FILE` maps runtime binding names
to reviewed readable names. `--selector-overrides FILE` maps source-unit IDs to
binding names and reviewed selectors; entries may use the normal
`source_matches` claim shape or a relational `members[].selector`. Overrides
must claim exactly the selected binding, and binding-name pins are rejected.
Omitted entries use selector synthesis. Existing claims move with their
selectors and metadata intact. Preview and write run the same final checks.

## Rejection diagnostics

Start with the reported source and constraint. If the investigation establishes
an author assertion's premises, use the [initialization contract
chooser](../skills/shared/initialization_contracts.md#choose-the-contract-for-the-established-fact)
to select its scope and audit obligations. A rejection alone does not establish
purity, callback timing, or a host/runtime premise.

When validation refuses a mutating command, the diagnostic names exactly
what's wrong:

**Atom split** (refused by the realizability gate). Lists each split atom:
which source units it covers, which modules its members would land in, and the
`DepKind` causes from the group (same data shape as `AtomicGroupConflict`).
The structured conflict report also includes a deterministic claim pair with
different destinations and shortest paths between them in both directions.
Path steps carry the dependency edge, its `G_atomic` projection, and whether
the path follows or traverses against that edge. A reverse step is permitted
only for a symmetric co-location constraint; it does not reverse a directed
initialization dependency. Each path reports its complete step count and keeps
at most twelve leading steps, with an explicit truncation flag.
When a sequenced side-effect edge is involved, the existing conflict/cycle
records also include each impure initializer source unit, its binding name, source
path and 1-based line/column, plus the `PurityRule`, optional detail, and any
author guidance. Each cut edge also carries a typed `explanation` of why the
gate treats it as constraining — the at-init read position for `eager_use`, or
the retained emission rule (`strict_chain`, `cell_order` with both endpoints'
effect classes and the conflicting cells, …) and the target side's impure
initializer for `sequenced`; the bail summary prints it under each binding-pair
row ([SPEC](../SPEC.md#realizability-cut-explanations)). When a
`commuting_statements` premise made `sequenced` edges between the SCC's members
inert, the summary adds an `Inert:` count line and up to three of those edges
with both endpoints, the premise's spec location and the retained cause; they
are never in the cut ([SPEC](../SPEC.md#commuting-statements)).
Unknown calls and constructors direct authors to synchronous precision losses
and the callee's source before choosing an audited initialization contract.
Purity assertions supply no missing call result, instance shape or heap
identity. Use the [contract chooser](../skills/shared/initialization_contracts.md#choose-the-contract-for-the-established-fact)
after establishing the required source fact.
The diagnostic does **not** auto-compute a minimal extra-moves completion —
it names the source units, destinations and one connecting witness so the author
can inspect how the selected claims are tied together.

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

Each cycle's `rejection` distinguishes `mutual_constraining_cycle`,
`esm_evaluation_tdz`, and `tdz_after_collapsing_pass_one`. The last runs Pass 2
after hypothetically co-locating every Pass-1 SCC and applies to that collapsed
assignment. Its owner list can include otherwise viable modules. When reducing
ownership, first try a candidate resolving the actual mutual/direct cycles,
then rerun the normal gate on that candidate. This can preserve a broad group
that would be lost by removing the union of every diagnostic's owners. Atomic
coverage and both gate checks still apply to the resulting assignment.

Edit-gate rejections (including `--dry-run` probes) also write those
artifacts to disk as siblings of `--graph` — the default location the
`gate` queries read — so the documented follow-up queries work on the
rejection that was just reported. The two artifacts may coexist, with the
atomic conflict remaining the primary verdict. After graph, assignment and
shared validation complete, the edit gate replaces prior rejection artifacts
with each nonempty current report; a passing assignment leaves neither file.
A preflight error leaves the last completed gate result intact. `debundle run`
(and `run --dry-run`) writes the same files under `reports/tree/<chunk>/`,
which is the same sibling-of-`dependency_graph.json.gz` layout.

A `commuting_statements` entry contradicted by a source fact
([SPEC](../SPEC.md#commuting-statements)) fails `run` with its diagnostic on
stderr; an entry inerting no edge is a `warning:` line instead and the run
continues unchanged. Whenever a report directory is written (`run`, `run
--dry-run`, `spec validate`), either lands in the chunk's
`commuting_statements_diagnostics.json`. `spec validate` reports them beside
the selector outcomes as `commuting_statements_diagnostics[]` (`kind` ∈
`contradicted` with `conflicts[]` and the endpoint source units; `unused` with
`reason`); only `contradicted` fails `validation.success`. The ndjson stream
emits each as a `commuting_statements_diagnostic` section. An `unused` entry
names its `spec_location` and, for each endpoint `a`/`b`, the authored
`selector`, the `logical_members` whose resolved declaration is that endpoint
(omitted when none) and its `source_location`. The text line prints the same
module/member names, or a `source_match` preview, and source position.

## Gate queries

The `gate` namespace names what is rejecting: the realizability gate. It
complements `scc` (which lists every quotient SCC, including singletons and
accepted multi-node ones); `gate` lists **only** the SCCs the gate
rejected. The unit is the blocking SCC, not a cycle — a single SCC can
contain exponentially many simple cycles, so the CLI exposes the cut (a
primitive on the SCC) but deliberately not a `cycle list`.

`scc` and `module_graph.sccs` describe graph structure; they do not decide
realizability. Their `constraining_module_edge_ids` lists initialization
constraints inside each SCC. An eager read in an import cycle can be safe
when its target has finished before the read. Use the shared gate's result
for acceptance or rejection, rather than treating a nonempty constraint list
as a rejection.

The SCC query commands (`gate list`, `gate describe`, and `gate cut`) accept
`--cycles <path>` to override the default `cycles.json` location (sibling of
`--graph`). They read a missing `cycles.json` as an empty set, so `gate list`
returns zero blocking SCCs and exit 0. Treat that as a clean result only when
the current gate report set is complete; rejection writers emit `cycles.json`
whenever the current assignment has blocking SCCs, including beside an atomic
conflict report.

For an actual Pass-1 mutual constraining SCC, each `cycles.json` entry carries
`pass_one_witness`: one complete ordered module closed walk anchored at a cut
edge. Each step uses the same source-unit IDs, binding labels and edge causes
as the cut; the JSON includes every step while the stderr preview gives its
omission count. The field is `null` for actual Pass-2 ESM TDZ and conditional
after-collapse diagnoses. A Pass-1 witness does not describe those paths.
Ownership-subset runs can use the stored witness without full graph export.

For a sequencing edge, `gate describe` retains the impure initializer source
unit and its purity reasons, and every cut and evidence edge carries the same
`explanation` the stored cut has (the evidence rebuilds it from
`dependency_graph.json.gz`'s `edges[].role` and `edges[].sequenced_cause`). Supply
`--modules` and `--source-root` together to get `sequenced_source_inspections`,
a runnable `inspect-source` command keyed by each stored source unit (source
and target sides alike). The text view prints the same commands and a `why:`
line per edge. Both `describe` and `cut` carry the SCC's
`inert_commuting_edges` (the `cycles.json` field: `sequenced` edges a
`commuting_statements` premise made inert, never in the cut and excluded from
`evidence`); the text view lists them after the cut, each with an `inert:`
line naming the premise. The graph and source snapshot must still match.

`gate diagnose <binding>` traces constraining promoted-at-initialization edges
to a matched callback-factory contract rejected by a supported static member
write, direct computed-key write through the imported namespace or a
recognized alias, or the exact namespace binding passed as a non-spread
argument of an ordinary top-level call. Other receiver shapes and guard families remain
inconclusive. It requires explicit graph, module-spec, and
source-root paths:

```sh
debundle gate diagnose later \
  --graph reports/tree/static/app/dependency_graph.json.gz \
  --modules spec/modules \
  --source-root snapshot \
  --format json
```

The JSON report connects the binding's current module ownership, promoted
dependency and callback source unit, matched contract signature, typed guard
code, and a source witness. Call-argument witnesses carry the exact call span,
argument index, and namespace binding. `opaque_call` is true only when an
`UnknownCallTarget` precision loss has the same source unit and call span; it
does not assert that mutation occurred. A supported row requires the edge's callee source unit to
match a statically resolved callback argument root for that call; otherwise it
reports an inconclusive source mismatch or unresolved callback source. Only
overloads whose arity and callback positions apply to the actual call are
reported. The write witness includes its graph source-unit ref and source-unit
ordinal. Diagnose does not read or validate the supplied source against the
graph-generation fingerprint, so it does not emit a parsed statement ID or
apply a possibly stale `SourceUnitOrdinal -> StatementId` crosswalk. Its
`inspection_command` is directly runnable and repeats all three paths. Output
is limited to five trace rows by default; `--limit N` changes that bound and
`--all` explicitly emits every row. `returned_count`, `total_count`,
`truncated`, `expansion`, `query_status`, and `coverage_scope` make the output
boundary explicit. `query_status` summarizes all uniquely linked candidate promoted edges,
including supported links beyond a bounded returned page. The query does not
establish the sole or globally earliest cause: unsupported guard families,
taint causes, and traces without a supported barrier remain explicitly
inconclusive. If one source unit has multiple candidate barriers, or a promoted
callback source does not match the callback argument roots of its barrier, the
query reports an inconclusive link rather than pairing a contract with the
wrong promoted callback.

## Proposing commutations (`spec propose-commutations`)

For a blocking SCC whose cut is `sequenced` edges with opaque effects, the
remedy may be a `commuting_statements` entry ([SPEC](../SPEC.md#commuting-statements))
rather than co-location. `spec propose-commutations` drafts those entries
mechanically from the rejection the pipeline or an edit gate wrote:

```sh
debundle spec propose-commutations \
  --graph reports/tree/static/app/dependency_graph.json.gz \
  --source-root snapshot \
  --modules spec/modules/static/app \
  --out commutations.yaml --format json
```

`--cycles` overrides the default `cycles.json` beside `--graph`; the common
`--graph` / `--source-root` / `--modules` compatibility rules apply, and
`--modules` is optional. For every blocking SCC the command:

- takes every constraining `sequenced` edge between the SCC's modules (gate
  view) — not only the `cut`, which is a feedback arc set chosen without
  regard to what a premise can inert, so the pair that breaks the SCC may sit
  on another arc — and classifies the pair of source units it joins by the
  verdict the graph build retained on every edge between them
  (`edges[].premise_verdict`, the classification the gate applies); a pair
  with a `known` conflict or one the gate would call unused is listed in
  `rejected` with a typed `reason` (`known_conflict` with the gate's
  conflicts, `unused_summarized`, `verdict_not_recorded` for a graph written
  before verdicts were retained, `cut_edge_not_in_graph`). `cut_edges` names
  the cut entries a pair covers, empty when the cut chose elsewhere;
- lists `eager_use` and other non-`sequenced` cut edges under
  `needs_co_location`: an at-init read is a dataflow dependence no premise
  inerts;
- ranks the candidates by effect, each pair once in the SCC where it ranks
  best (`also_in_sccs` names nested SCCs it also lies in): a `single_pair_cut`
  (inerting this pair alone leaves the SCC's constraining subgraph acyclic,
  every parallel unit edge counted and third-statement order preserved by
  the same closure as graph construction), then one that empties its module-pair
  arc (`remaining_module_pair_edges` 0), then more inerted edges, then one the
  gate's cut names;
- emits the best `--limit` (default 50; `--all` for every one) proposals,
  proving one selector per endpoint with the selector codemod and the shared
  resolve: `resolved_member` for a logical member when `--modules` names the
  chunk's tree (its authored module spelling comes from the tree), otherwise
  a `source_match` claiming the declared binding, or the exact statement for
  an anonymous one. An endpoint with no uniquely proving selector is reported
  as `no_unique_selector` with a typed `detail` object retaining the resolve's
  outcome or the endpoint failure, and the pair is not emitted. Past the limit an anonymous endpoint is still proven (one resolve),
  but a declared-binding endpoint (a minimizing search) is assumed
  selectable; each SCC row's `unproven_endpoints` counts those. A failed
  selector would only keep its pair's edges constraining, so
  `pairs_cannot_break` holds regardless, with its counts a lower bound, and
  `pairs_break` is conditional on them. `--all` proves every endpoint (about
  75 s on canonical Tana web without `--modules`). `--limit` also caps
  `rejected` and `needs_co_location`, and `totals` gives each list's full
  size;
- inerts every proposal of the SCC together and writes one `sccs` row per
  SCC (its `modules` and a `verdict`): `pairs_break`, or `pairs_cannot_break`
  with `residual_modules` (the modules still on a constraining cycle),
  `residual_components` (one `{modules: [...]}` row for each remaining
  nontrivial cyclic component; `residual_modules` is the sum of their member
  counts), and `blocking_edges`, the constraining edges
  between them counted by `kind` and `reason` (`kind_not_inertable` for
  `eager_use` and the other kinds no premise inerts, else the rejection reason
  of the edge's pair, or `preserved_order` for an additional third-statement
  constraint). Combined preservation stops once a remaining cycle is proved;
  `counts_are_lower_bounds: true` then marks the counts and cyclic memberships
  as proven lower bounds rather than the complete final inventory. This avoids
  expanding every transitive relation of a long chain merely to prove failure.
  A `pairs_break` result requires completed preservation. Module paths in each residual component, and component
  rows by their member paths, are sorted deterministically. The text report
  prints the same member paths. `pairs_break` has no `residual_components`
  field. An SCC with no proposal still gets its row.

Each proposal carries the `entry` to paste, both endpoints (source unit,
binding, module, source location, the source unit's first line — its own
declarator in a comma-list declaration — and `selector_origin`), the
`inerted_edges` with their `SequencedCause` explanation, and the `scc_id` of
the SCC it cuts. `draft_yaml` is the `chunk_analysis_options.<chunk>.commuting_statements`
fragment in rank order, each entry preceded by the same facts as comments and
`note` left `null` for the author's rationale; `--out` writes it to a file.
The command is read-only analysis: it never writes into the spec, and an
applied entry is validated by the gate's own contradiction and unused checks
on the next `run` or `spec validate`. Runtime failures follow the typed
`reason` convention of § Runtime failures from top-level queries.

## Workflow: investigating a binding end-to-end

When a binding's role or a candidate boundary is unclear:

For bounded owner discovery, choose the first page for the question: use
`graph-summary --limit N` or `atoms --residual-only --limit N` to inspect large
residual atomic groups. Put the owners you want to investigate in a JSON array, then use
`describe --batch owners.json --direction incoming --limit N` (repeat
`--reason KIND` when useful). Each report's `edge_census` counts raw incoming
and outgoing edges by dependency kind and role after direction/reason filters;
an omitted raw role is reported as `direct`, and counts stay complete when edge
detail rows are limited. The two directions are separate incidence counts, so
an edge between two selected source units can appear once in each. Do not sum
them as unique edges. Atomic-group and module-quotient edges have no census role:
those aggregate projections do not retain the raw edge role. This filtered
inspection helps choose what to read next; it is not a gate verdict.

1. **`debundle describe <sym>`** — graph + spec context: the binding's
   source unit, home module, atom membership, and incoming/outgoing edges. Add
   `atoms` and `coverage` for additional ownership and structural context.
2. **`debundle inspect-source --id <sym>`** — inspect the source unit's numbered
   source item. Use `--view raw` for original bytes and
   `--context-statements 1` for neighboring items.
3. **`debundle cluster <sym>`** — list the module-quotient neighbors of the
   binding's source unit. Useful for "what does this module touch?" questions
   before deciding a destination.

### Explain behavior with a partial spec

Trace from the relevant entry point through branches, helpers, data, and callers.
Use emitted modules for readability and the inspection workflow above for source
and graph evidence. Coverage counts alone do not establish behavior.

When answering requires missing coverage and spec edits are in scope, claim only
the complete atomic groups needed for the question, using the
[extraction recipes](spec_editing.md). Choose coherent module boundaries,
[descriptive names](naming.md), and [purpose-bearing selectors](selectors.md).
A current unique match cannot prove future rebuild stability. Record factual
module/binding comments through `modules comment` / `bindings comment` when they
clarify behavior; YAML `#` comments do not emit into the generated JS.

Run the adapter's full pipeline/gate after edits and exercise emitted behavior
with its applicable runtime checks. Regenerate through the adapter. Fix failures
before relying on the changed tree, or state what remains unverified. If source
or spec is unavailable, answer from available evidence without claiming that the
debundled tree covers it.

Explain the observed control/data flow and cite the generated paths/functions
that demonstrate it. Report spec additions, actual gate/runtime results, and
remaining uncertainty. Keep a focused question scoped to the code needed for its
answer; [specialist roles](../skills/shared/workflow.md#roles) handle substantial
source discovery, architecture, naming, or multi-lane extraction.

## Evidence files

Typical debundle outputs:

- executable JS under `app/`
- root reports under `reports/`: `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, `vendor_swaps.json` when those outputs are
  configured
- `reports/tree/program_graph.json.gz`: the program-wide graph sections every
  chunk's `dependency_graph.json.gz` references (input provenance, native
  mutation census)
- per-chunk reports under `reports/tree/<chunk-id>/`: `chunk.json`,
  `modules.json`, `dependency_graph.json.gz`, `export_reentry.json`,
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

### Synchronous execution precision

In `dependency_graph.json.gz`, inspect a node's `synchronous_execution.complete`
and `precision_losses` to see why synchronous-effect inference retained a
conservative fallback. Reasons distinguish unknown call targets, unsupported
operations, missing call results, recursion, and exhausted invocation or callback-pool budgets.
`missing_call_result` means the call's synchronous-effect proof completed but
supplied no return-value transfer for the ordered body. Continue implementation
at that call's operand/result transfer; adding a timing premise or replaying its
source operands does not supply the missing value. Budget
records include their limit; each loss carries its lexical statement ordinal and,
when available, its source location. Nested reasons retain their original source
site. An `unsupported_operation` / `opaque_execution` loss may also carry
`ordered_call_admission_refusal`, which distinguishes a declined unknown,
multiple, or non-local target from an unsupported
receiver. This evidence is separate from purity reasons and does not certify
author contracts. See [the report contract](../SPEC.md#synchronous-execution-diagnostics).

For a directly called imported bare callback factory, read the node's
`synchronous_execution.callback_factory_uses`, then resolve each `summary_ref`
in the same graph's `imported_callable_summaries`. The use names the lexical
call site and the top-level source unit containing it names the active analysis.
The table preserves asserted and inferred evidence without requiring
`modules.json`. Use records show where the callback-factory capability was
applied. Alias and joined-value calls are not attributed; absence does not
establish an unused annotation. See the
[report contract](../SPEC.md#synchronous-execution-diagnostics).

### Native integrity guards

`debundle explain-integrity` reads a saved graph and its `program_graph.json.gz`
(no reanalysis) and answers why the census leaves an intrinsic slot unknown
(SPEC.md § Native integrity inputs):

```bash
# Losses on this line, the units whose execution recorded them, and their failed guards
debundle explain-integrity --graph output/reports/tree/static/index/dependency_graph.json.gz \
  --at static/index.js:1234
# The same for a source unit or a binding it declares
debundle explain-integrity --id unit:120
# Bound precision-loss examples per source unit and witness rows per slot
debundle explain-integrity --id unit:120 --limit 5
# Show all rows
debundle explain-integrity --id unit:120 --all
# Every census witness reaching one slot
debundle explain-integrity --slot '%Array.prototype%'   # or Array.prototype.map, Array[@@species], Object.defineProperty, Set
```

A source-unit row lists the guards its execution evaluated and, for each
failed one (`native_integrity_checks`), the slots it required and which of
them are unknown. Each unknown slot then lists its witnesses with location,
excerpt, operation and `reach`:

- `named_write`: the write's receiver and key name the slot, or it assigns the
  slot's global binding.
- `family_coupling`: a prototype slot is family-level, so any write to its
  family (`Object.entries = f` for `%Object.prototype%`) makes it unknown.
- `global_object_coupling`: a global-object write (`window.flag = 1`) makes
  every whole-prototype slot unknown, even for a bundle-owned property. It
  never reaches a single prototype property (`%Array.prototype.map%`) or
  `@@species` slot, which take the own-slot reach kinds.
- `unknown_key`: an exact receiver whose key may name the slot.
- `unknown_receiver`: a receiver that may be any native object.

`receiver_global` names the global property an unknown receiver was read from
(`location` for `window.location.href = url`). `possible_discharges` lists
author premises or host keys that could remove the witness from this slot's
census. These are audit candidates only: they do not assert that a matching
input is configured, independently audited, or used as proof.

`program_premises.intrinsics_never_written` can override an
`unknown_receiver`, `family_coupling` or `global_object_coupling` witness for a
named family, as defined by the premise contract. The contract rejects a write
to a named family's exact object or binding; it overrides other witnesses
that would reach the family (SPEC.md § Intrinsics never written). Because the
candidate list is based on reach kind, it may include `intrinsics_never_written`
for a family-level witness whose exact writer makes that premise invalid. Check
the witness against the contract before authoring a premise. For
`unknown_receiver`, `possible_discharges` can also include
`predefined_globals: standard` when a host-undefined global property is the
receiver, `no_accessor_install_callables` when a computed-key call keeps that
property from qualifying as bundle-owned, or `value_inputs` when a local
binding captures an ordinary object (SPEC.md § Bundle-owned globals and §
Captured ordinary-object values). `native_operation_inputs` applies separately
to each selected consuming site; `map_empty` can select every direct `new Map()`
data field of one checked object initializer. It does not discharge a census witness (SPEC.md §
Native integrity inputs).
For `set_from_primitive_array`, a dense array may contain primitive literals
and earlier direct top-level primitive-literal `var`/`let`/`const` bindings
whose canonical identities have no other declaration or write. The exact
constructor site still needs its native protocol input; see
[the source proof](../SPEC.md#native-integrity-inputs).

`--limit` bounds precision-loss examples **per source unit** and witness rows
**per slot**. Under `--at`, the line filter is applied before the per-unit
limit. Each source-unit row's `precision_loss_limit` and each slot's
`witness_limit` report exact `total`, `emitted` and `truncated` values; the text
report states when it omits rows. `--all` shows every row. Source-unit and slot
selection, guard findings and `reach_counts` remain complete.

When a purity reason is `unknown_member` for a recognized native own-data read,
its `detail` names the required slots, any slots without proof, and an
`explain-integrity --slot` query for each missing slot. Run
`debundle explain-integrity --graph <dependency_graph.json.gz> --slot '<slot>'`
once per missing slot. If the detail reports a complete-slot-set or
dynamic-scope failure, query each required slot. The command reads the shared
`program_graph.json.gz` census and shows its blocking witnesses. A computed
member, a shadowed intrinsic binding, or an unrecognized property is labeled
without inventing a native slot query.

The command does not attribute an individual precision loss to a guard: it
shows the guards that failed during the same source unit's execution. Callable
and own-property certificates that need intact slots fail without a check row.

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
`comment:`. JSON outcomes use `action` values `read`, `set`, `cleared`,
`unchanged`, or `dry-run`. Non-emitting `note:` fields are YAML-authored metadata.

## Out of scope

- **No cross-process materializer reader.** `debundle run` reads the spec
  and emits JS in one process — see `wire_format.md` § "Why pre-filter
  facts (`StatementFacts`) aren't on the wire" and
  `lessons_learned/cross_process_stage_b.md`.
- **Shell tab completion.**

## See also

- `design.md` — the realizability theorem the gate enforces; § "Layered
  mental model" + § "Factor assembly inside `debundle run`" for the materializer.
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
