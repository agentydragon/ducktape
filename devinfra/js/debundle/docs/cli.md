# debundle CLI

Cross-command semantics of the `debundle` CLI: conventions, pipelines, and
interactions that span commands. The per-command and per-flag reference is
the clap doc-comments themselves — `debundle <command> --help` — and the
code wins wherever prose and binary disagree. Future CLI ideas live in
`TODO.md`.

The CLI is one binary (`bazel run @ducktape_debundle_bin//file:debundle` or
built locally as `bazel-bin/devinfra/js/debundle/debundle`). All commands
share the same JSON-on-stdout / structured-diagnostic-on-stderr convention
as the rest of ducktape.

## Command index

- Pipeline: `run` (mutates: emits JS + reports)
- Spec edits (mutate the modules tree):
  `bindings {assign,unassign,rename,comment}`,
  `modules {merge,delete,comment}`,
  `spec {selector-codemod,synthesize-selectors}` with `--apply`
- Read-only queries: `bindings list`, `modules {list,propose}`,
  `spec {stats,selector-debt,match-selector,validate}`, `atoms`, `coverage`,
  `graph-summary`, `describe <id>`, `show-source <id>`, `scc`,
  `cluster <sym>`, `gate {list,describe,cut}`, `selector-solve`

## Common arguments and env vars

Three common paths show up on most commands. Each accepts both a flag and an
env var; the flag wins if both are set.

| Flag                  | Env var                | Meaning                                                                                                                                                     |
| --------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--graph <path>`      | `DEBUNDLE_GRAPH`       | `owner_graph.json` for the chunk being inspected or edited. The graph path implies the chunk; multi-chunk callers point at different graphs per invocation. |
| `--modules <dir>`     | `DEBUNDLE_MODULES`     | Per-module YAML tree root (the directory under `spec/modules/`).                                                                                            |
| `--source-root <dir>` | `DEBUNDLE_SOURCE_ROOT` | Upstream snapshot root containing the original chunk bytes.                                                                                                 |

`debundle run --out-root` reads `DEBUNDLE_OUT`. `debundle run
--tree-source-root` (the spec-tree compile root for source-relative paths in
the tree-shaped config) reads its own env var, `DEBUNDLE_TREE_SOURCE_ROOT` —
deliberately **not** `DEBUNDLE_SOURCE_ROOT`, because the two roots are
different directories in real corpora (spec tree vs. upstream snapshot).

Export the env vars once per shell session and subsequent commands run
without repeating the flags:

```bash
export DEBUNDLE_GRAPH=<debundle-output>/reports/tree/<chunk-id>/owner_graph.json
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
propose` is the command that runs the proposal factorizer by default;
`describe`, `coverage`, and `graph-summary` do not run it unless the
selection itself is a proposal/diagnostic id or `--include-proposals` is
passed (it is expensive on large graphs), and proposal-derived JSON fields
are omitted when the factorizer is skipped.

The five mutating verbs (`bindings assign`, `bindings unassign`,
`bindings rename`, `modules merge`, `modules delete`) take the same
`--format` flag with the same tty/pipe default. Under `text` they print a
one-line verdict (`<action>` plus move/file counts). Under a JSON format
each verb prints **one outcome object** sharing a common schema core:

| Field           | Values                                                                                                                            |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `verb`          | `assign` \| `unassign` \| `rename` \| `merge` \| `delete`                                                                         |
| `action`        | `applied` \| `dry-run` \| `noop` \| `unchanged` \| `rejected`                                                                     |
| `gate`          | `passed` \| `names_only` \| `skipped` (`--no-verify`) \| `not_required` (edit cannot change the partition); success outcomes only |
| `files_written` | Files written (or, under `--dry-run`, that would be written)                                                                      |
| `files_deleted` | Files deleted (or, under `--dry-run`, that would be deleted)                                                                      |

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

`describe` and `show-source` take any ID kind and dispatch on shape:
bindings (minified or readable), module paths (`runtime/plugins`) or
unambiguous module filenames, module ids (`logical:7`), owner ids
(`owner:42`), atom ids (`atomic:N`), proposal ids, diagnostic ids.

## Validate-by-default (mutating commands)

Every command that modifies the spec runs validation **by default** before
writing changes back to disk. For commands that affect the chunk's
factorization (anything that moves a binding between modules), that means
the full realizability + atom-split gate — which requires
`--graph <owner_graph.json>` unless `--no-verify` is set; for renames, it
means name-collision detection; for comment edits, shape preservation only
(`--no-verify` is a no-op there). If validation fails the command refuses
with a structured diagnostic and **does not modify any file**.

Two flags adjust the default on spec-edit commands:

| Flag          | Effect                                                                                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (default)     | Validate; refuse the change if invalid; apply if valid.                                                                                                       |
| `--no-verify` | Skip validation; apply the change regardless. Escape hatch for multi-step refactors where an intermediate state is intentionally invalid. Don't use casually. |
| `--dry-run`   | Validate (or simulate) but do not modify any file. Print the validation result + a diff summary.                                                              |

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
side outputs (`cycles.json`, `atomic_unit_conflicts.json`) under
`reports/tree/<chunk-id>/`. `debundle run --dry-run` runs pipeline
parse/facts/gate checks without writing emitted JS or reports; a gate
rejection still writes `owner_graph.json` plus the rejection evidence, so
the `gate` queries work on the rejection that was just reported.

Broad spec migrations continue through supported diagnostic failures by
default and report all findings from that pass (currently: unresolved
source-match selectors and duplicate binding claims, with
module/export/origin evidence). Use `--fail-fast` only when the first
failing selector or claim is the useful debugging target.

`debundle spec validate` is `debundle run` in dry-run keep-going mode
reporting every selector problem: it takes the **same inputs** (`--spec` /
`--tree-config` + package roots) and needs the full pipeline, so run it via
the Bazel `:debundle` target, not the standalone binary. Its source-only
preflight mode (`--modules` plus `--source-file` or `--source-root
--chunk`) needs only the binary and the chunk.

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
   claimed.
3. Run the realizability gate on the post-batch spec.
4. If invalid: print binding-pair blame, exit non-zero, **do not modify any
   file**.
5. If valid: write every changed surviving YAML first, then delete drained
   move-source files — an interruption can never lose a member that was not
   yet written to its destination.

`--dry-run` runs steps 1–4 and stops. `--no-verify` skips step 3 (still
does duplicate-claim detection — that's a structural error, not a
validation one). `bindings unassign` shares the same post-batch validation
and drain sweep.

### `--batch` JSON format

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
selected proposal is a direct member move:

- `landable_today: true`
- non-empty `binding_ids`
- no `merge_into`
- no `anonymous_statement_owner_ids`

Fresh proposals move each `binding_ids[]` entry to `proposed_module_id`;
extension proposals move them to `extends_module_id`. `merge_into` rows
require `modules merge` or manual YAML because they combine existing source
modules. Rows with `anonymous_statement_owner_ids` require
`anonymous_statements:` edits, which `bindings assign` does not perform.
Rows with `status: blocked_residual_dependency` carry
`landable_today: false` and are rejected: grow the closure (assign them
together with the residual cells they reference) or co-locate the owners
manually first.

## Rejection diagnostics

When validation refuses a mutating command, the diagnostic names exactly
what's wrong. Two kinds:

**Atom split** (refused by the realizability gate). Lists each split atom:
which owners it covers, which modules its members would land in, and the
`DepKind` causes from the unit (same data shape as `AtomicUnitConflict`).
The diagnostic does **not** auto-compute the minimal extra-moves completion
— it names the owners and destinations so the author can read the
completion off the printed atom membership.

**Name collision** (refused by `bindings rename` or by `bindings assign`
when a `:readable` field collides). Lists each collision: the existing
binding holding the name, the binding the rename would have given it.

Both diagnostic shapes go to stderr; the command exits non-zero. Under a
JSON format a realizability-gate rejection additionally prints one
structured object on stdout: `{verb, action: "rejected", rejection}`, where
`rejection.kind` is `atom_split` (with `conflicts`, the canonical
`AtomicUnitConflictReport` projection) or `unrealizable_cycles` (with
`blocking_sccs`, the canonical `BlockingSccEntry` projection). These are
the **same wire shapes** `atomic_unit_conflicts.json` / `cycles.json` carry
— there is no parallel rejection schema.

Edit-gate rejections (including `--dry-run` probes) also write those
artifacts to disk as siblings of `--graph` — the default location the
`gate` queries read — so the documented follow-up queries work on the
rejection that was just reported. A subsequent edit that passes the gate
clears the stale artifacts. `debundle run` (and `run --dry-run`) writes the
same files under `reports/tree/<chunk>/`, which is the same
sibling-of-`owner_graph.json` layout.

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

## Module rename / disable

Renaming or disabling a module is **not** a CLI operation — it's a plain
`mv` on the YAML file. The spec compiler infers the module path from the
file location:

```bash
# Rename: the module path is re-derived from the new filename.
mv $MOD/runtime/plugins.yaml $MOD/runtime/plugin_settings.yaml

# Disable: any non-.yaml suffix makes the compiler skip the file.
mv $MOD/runtime/plugins.yaml $MOD/runtime/plugins.yaml.disabled
```

After the `mv`, the next `debundle run` (or any subsequent mutating command
on the spec) re-validates and surfaces any resulting atom split as a gate
diagnostic.

## Workflow: investigating a binding end-to-end

When a binding's role is unclear or a proposal is suspicious:

1. **`debundle describe <sym>`** — graph + spec context: the binding's
   owner, home module, atom membership, and incoming/outgoing edges. Add
   `--include-proposals` only when factorizer proposal/diagnostic
   annotations are needed.
2. **`debundle show-source <sym>`** — print the original source span for
   the owner. Use `--context-lines 40` to widen the view.
3. **`debundle cluster <sym>`** — list the module-quotient neighbors of the
   binding's owner. Useful for "what does this module touch?" questions
   before deciding a destination.

## Evidence files

Typical debundle outputs:

- executable JS under `app/`
- root reports under `reports/`: `output.json`, `chunks.json`,
  `runtime.json`, `source_assets.json`, `provenance.json`,
  `rename_queue.json`, `vendor_swaps.json` when those outputs are
  configured
- per-chunk reports under `reports/tree/<chunk-id>/`: `chunk.json`,
  `modules.json`, `owner_graph.json`, plus `cycles.json` /
  `atomic_unit_conflicts.json` only when validation rejects
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

The `comment:` / `note:` schema (which levels carry them, what emits into
generated JS vs. stays YAML-only, the no-`#`-comments rule, and the
`modules merge` composition) is documented once in <../README.md> →
"Comments"; the editing workflow in `spec_editing.md` → "Workflow:
authoring `comment:` fields". The CLI surface is `bindings comment` /
`modules comment` — they edit emitting `comment:` fields; non-emitting
`note:` fields are YAML-authored metadata that the rewriters preserve.

## Out of scope

- **No cross-process materializer reader.** `debundle run` reads the spec
  and emits JS in one process — see `wire_format.md` § "Why pre-filter
  facts (`StatementFacts`) aren't on the wire" and
  `lessons_learned/cross_process_stage_b.md`. `facts.json` is an
  in-process debug artifact, not a CLI input (`facts/wire.rs`).
- **Auto-computed minimal completion** for atom-split rejections — worth
  revisiting once the basic CLI surface is in use.
- **YAML diff in `--dry-run`.** v1 prints only the verdict line and
  planned file set; a structured diff (post-mutation YAML preview) is a
  documented TODO in the codebase.
- **Tab completion.** Not in v1.

## See also

- `AGENTS.md` — generic operator workflows that compose these commands.
- `design.md` — the realizability theorem the gate enforces; § "Layered
  mental model" + § "Factor assembly inside `debundle run`" for the
  factorization algorithm `modules propose` draws from.
- `wire_format.md` — JSON sidecar conventions readers of these commands
  consume.
- `selectors.md` / `spec_editing.md` — selector authoring and spec-editing
  workflows.
