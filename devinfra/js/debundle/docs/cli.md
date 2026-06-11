# debundle CLI

Reference for the current `debundle` CLI command surface. Future CLI
ideas live in `TODO.md`; this file should describe commands that exist
today.

The CLI is one binary (`bazel run @ducktape_debundle_bin//file:debundle`
or built locally as `bazel-bin/devinfra/js/debundle/debundle`). All
commands share the same JSON-on-stdout / structured-diagnostic-
on-stderr convention as the rest of ducktape.

## Convention: validate-by-default for mutating commands

Every command that **modifies the spec** (`bindings assign`,
`bindings unassign`, `bindings rename`, `modules merge`) runs
validation **by default** before
writing changes back to disk. For commands that affect the chunk's
factorization (anything that moves a binding between modules), that
means the full realizability gate; for renames, it means name-
collision detection. If validation fails the command refuses with a
structured diagnostic (binding-pair blame for gate rejections, name
clash for renames) and **does not modify any file**.

Two flags adjust the default on spec-edit commands:

| Flag          | Effect                                                                                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (default)     | Validate; refuse the change if invalid; apply if valid.                                                                                                       |
| `--no-verify` | Skip validation; apply the change regardless. Escape hatch for multi-step refactors where an intermediate state is intentionally invalid. Don't use casually. |
| `--dry-run`   | Validate (or simulate) but do not modify any file. Print the validation result + a diff summary.                                                              |

`--dry-run` and `--no-verify` can be combined: show what would
change without validating. Mostly useful when investigating _why_
the gate would reject and you want to inspect the intermediate
state without committing to it.

`debundle run --dry-run` is separate: it runs pipeline parse/facts/gate
checks and skips all emitted JS and report writes. There is no
`debundle run --no-verify`.

Read-only commands (queries, listings, source slicing) take
neither flag — they have no side effects.

`run` is a special case: the gate is part of the pipeline contract,
not an optional pre-check. There is no `run --no-verify` — if you
want the pipeline to emit JS regardless of the gate, fix the spec
first.

## Name resolution

Every command that takes a binding name (`<sym>`) accepts **either
form** wherever the lookup is unambiguous:

- The _minimized_ name from the chunk (e.g. `XOe`) — the stable
  hygiene-aware identity.
- The _readable_ name from the spec's `name:` field
  (e.g. `PluginSettingsAccessor`).

If both forms could match different bindings, the command refuses
with a list. Use the minified form to disambiguate.

## Command table

### Pipeline

| Command        | Mutates?                 | Function                                                                                                                                                                                 |
| -------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `debundle run` | yes (emits JS + reports) | Run the full transform pipeline: parse + facts + owner_graph + atomic_units + realizability gate + lower + emit. `--dry-run` runs pipeline checks without writing emitted JS or reports. |

### Bindings

| Command                                                           | Mutates?   | Function                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ----------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `debundle bindings list [--in <module>] [--unrenamed] [--orphan]` | no         | List bindings in the chunk with summary stats (home module, current readable name if any, atom-membership flag). Filters available for the common reverse-lookup cases (e.g. `--unrenamed` to find bindings still using the minified name).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `debundle bindings assign <sym>:<module>[:<readable>] …`          | yes (spec) | Move one or more bindings into named logical modules **atomically**. Each positional argument is colon-separated: `<sym>:<module>` to move and keep the current name, `<sym>:<module>:<readable>` to move and rename in the same step. `<sym>` accepts minified or readable form; the optional third field always sets the new readable `name:`. **Neither `<sym>` nor `<readable>` may contain `:`** — use `--batch` JSON for any edge case where they do. `--batch <file.json>` (or `--batch -` for stdin) reads moves as a JSON array of `{sym, module, readable?}` objects, or reviewed binding-only `modules propose` rows that map directly to member moves. Validation runs on the _whole batch's_ post-state and includes the realizability + atom-split gate (same predicate `modules merge` / `modules delete --force` use); the gate requires `--graph <owner_graph.json>` unless `--no-verify` is set. Destination modules are auto-created if they don't yet exist; source modules that become empty after the move are deleted. Default: validate + apply atomically. `--no-verify` / `--dry-run` available. See "Batch atomicity" below. |
| `debundle bindings unassign <sym> …`                              | yes (spec) | Remove one or more bindings from their current modules **atomically**; they fall through to residual (the default when an owner isn't claimed by any spec module). `<sym>` accepts minified or readable form, same resolution as `bindings assign`. Validation runs on the post-batch spec and includes the realizability + atom-split gate (same predicate `bindings assign` / `modules merge` use); the gate requires `--graph <owner_graph.json>` unless `--no-verify` is set. Splitting an atom by unassigning only some of its members is rejected; unassigning a whole atom together is accepted. Source modules drained to zero members are deleted unless they carry a module-level `comment:` or remaining `anonymous_statements:`. Default: validate + apply atomically. `--no-verify` / `--dry-run` available; dry-run and apply share an exit code on the same input.                                                                                                                                                                                                                                                                       |
| `debundle bindings rename <original> <readable>`                  | yes (spec) | Rename a binding's readable `name:` without moving it. `<original>` accepts minified or current readable form. Neither `<original>` nor `<readable>` may contain `:`. Validation here is name-collision detection (no two bindings in the chunk get the same readable name). Mostly a convenience over `bindings assign` for the rename-without-move case. `--no-verify` / `--dry-run` available.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `debundle bindings comment <sym> [text] [--edit] [--clear]`       | yes (spec) | Set, read, or clear a binding's `comment:` field. Positional `"text"` replaces the comment with the literal arg. `--edit` opens `$EDITOR` (fallback `$VISUAL`, then `vi`) on a temp file pre-populated with the current comment. `--clear` removes the field. With no extra args, prints the current comment (plain text on tty, JSON on pipe). An unset comment reads as `null` in JSON (an empty line in text), distinct from an explicit `comment: ""`. `<sym>` accepts minified or readable. Validation is shape-preservation (YAML still parses); comments don't affect factorization, so `--no-verify` is a no-op. `--dry-run` previews without writing. Member comments emit as JS comment blocks above the binding's owner statement.                                                                                                                                                                                                                                                                                                                                                                                                           |

### Modules

| Command                                                                                                      | Mutates?   | Function                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------ | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `debundle modules list [--empty] [--residual] [--unassigned-bindings] [--auto-deletable] [--with-anonymous]` | no         | List all modules in the spec with summary stats (`member_count`, `anonymous_statement_count`, `residual` flag, `has_comment`). `--empty` filters to **truly empty** modules — no members AND no `anonymous_statements:` — matching `modules delete`'s default-deletable predicate. A module with `anonymous_statements:` only is not empty in any meaningful sense (its side effects still rebuild every run) and isn't reported. `--unassigned-bindings` uses the same predicate. `--auto-deletable` narrows further to truly-empty **and** no module-level `comment:` — the subset safe to sweep with `modules delete`, since a comment would pin the module as a kept shell. `--with-anonymous` adds the `anonymous_statement_count` to text output (JSON always carries it), surfacing residual-sentinel side-effect drift. `spec stats`'s `modules.empty` counter applies the same definition. |
| `debundle modules merge --target <T> <sources...>`                                                           | yes (spec) | Splice `members:` + `anonymous_statements:` from each source YAML into `<T>`; create `<T>` if needed; delete the sources. Module-file args may include or omit the `.yaml` suffix, so `ai/models/pricing` resolves to `ai/models/pricing.yaml` even when `ai/models/pricing/` is also a directory. Default: validate (realizability gate against the post-merge partition) + apply. `--no-verify` / `--dry-run` available. The gate requires `--graph <owner_graph.json>` unless `--no-verify` is set.                                                                                                                                                                                                                                                                                                                                                                                              |
| `debundle modules delete <path>...`                                                                          | yes (spec) | Delete one or more module YAML files. Module-file args may include or omit the `.yaml` suffix. Default refuses non-empty modules (with members or `anonymous_statements:`); pass `--force` to override. Multiple paths delete atomically (all paths are validated up front; if any check fails, nothing is deleted). `--no-verify` / `--dry-run` available. The realizability gate fires on the post-delete spec; for the all-empty case the check is trivial (no edges change). For `--force` deletions of non-empty modules the gate requires `--graph <owner_graph.json>` unless `--no-verify` is set.                                                                                                                                                                                                                                                                                           |
| `debundle modules propose`                                                                                   | no         | Emit factorizer proposals (binding → module assignment recommendations) + diagnostics derived from the atomic DAG. Read-only — surfaces _suggested_ assignments; applying them requires `bindings assign`. Anonymous-statement proposals stay visible even when they are not directly addressable; with `--source-root`, JSON annotates `landable_today`, `unaddressable_anonymous_owner_ids`, and `landability_notes` using full-JS-AST selector uniqueness.                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `debundle modules comment <module> [text] [--edit] [--clear]`                                                | yes (spec) | Set, read, or clear a module's top-level `comment:` field. Same three modes as `bindings comment` (positional text / `--edit` / `--clear`; bare invocation reads). `<module>` is the module path relative to `$MODULES` (e.g. `runtime/plugins`). Module-level comments protect a module from auto-delete when `bindings assign` drains its members and emit at the top of the generated module file. `--no-verify` is a no-op; `--dry-run` previews without writing.                                                                                                                                                                                                                                                                                                                                                                                                                               |

Renaming or disabling a module is **not** a CLI operation — it's a
plain `mv` on the YAML file. The spec compiler infers the module
path from the file location, so:

```bash
# Rename: the module path is re-derived from the new filename.
mv $MOD/runtime/plugins.yaml $MOD/runtime/plugin_settings.yaml

# Disable: any non-.yaml suffix makes the compiler skip the file.
mv $MOD/runtime/plugins.yaml $MOD/runtime/plugins.yaml.disabled
```

After the `mv`, the next `debundle run` (or any subsequent mutating
command on the spec) re-validates and surfaces any resulting atom
split as a gate diagnostic. No dedicated `modules rename` /
`modules disable` subcommand — the filesystem operation is already
the right primitive.

### Spec-wide

| Command                       | Mutates? | Function                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `debundle spec stats`         | no       | Emit spec-wide summary in JSON (or text/ndjson). One pass over the modules tree: `modules` totals (`total`, `residual`, `empty`, `with_comment`) + `member_count` buckets (`min`, `max`, `singletons` = exactly 1 member, `tiny_2_to_5` = 2..=5, `medium_6_to_20` = 6..=20, `large_21_plus` = 21+), plus `bindings` totals (`total`, `renamed`, `unrenamed`, `orphan`, `with_comment`). Same per-row counters as `debundle modules list` / `debundle bindings list`; spec stats just folds them into one report so callers don't `jq`.                                                                                                                                                                                                                                                                                                             |
| `debundle spec selector-debt` | no       | Rank the spec's rebuild-fragile selectors. Three sections: **name-only** selectors (`selector.binding`) scored by how minified the bound name looks (1-2 chars / vowel-less rank highest), most fragile first; **repeated source_match** bodies copied verbatim (whitespace-insensitive) across members / anonymous statements / binding groups — the copies a contextual selector should replace; and, with `--against <other modules tree>`, **drifted bindings** — members whose readable `name:` is stable but whose `selector.binding.name` changed between the two spec versions. `--min-score N` filters the name-only list (`0..=100`; the summary keeps spec-wide totals); `--limit N` caps rows per section. Reads only the modules tree — no graph/source needed. `ndjson` emits one tagged object per row plus a final `summary` line. |

### Quotient queries

| Command                                                                                | Mutates? | Function                                                                                                                                                                                                                                                                                                                                               |
| -------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `debundle scc [--binding <sym>] [--cycles-only] [--residual-only] [--singletons-only]` | no       | List SCCs in the module-quotient graph. Filter to a single binding's SCC or to a specific class. (Streaming output via `--format ndjson` — see "Output format" above.)                                                                                                                                                                                 |
| `debundle cluster <sym>`                                                               | no       | List the module-quotient neighbors of a binding's owner. `<sym>` may be passed positionally or as `--binding <sym>`. Each neighbor (and the home module) is reported as both its interned `logical:N` id and its human path `label`, so output is legible without a second `describe`.                                                                 |
| `debundle gate list`                                                                   | no       | List every blocking SCC in `cycles.json`. One row per entry: id, modules count, cut size. (Streaming via `--format ndjson`.) A missing `cycles.json` is the clean state — the pipeline writes it only on rejection — so it reports zero blocking SCCs (`[]`) and exits 0, distinct from a present-but-malformed file (which errors).                   |
| `debundle gate describe <id> [--binding <sym>]`                                        | no       | Full picture for one blocking SCC: modules list, cut, plus the per-edge evidence recomputed on demand from `owner_graph.json` and the SCC's module set. Renders the same per-binding-pair blame view the realizability gate emits to stderr at rejection time. `--binding` narrows evidence to one symbol's contribution (handy for 1000-module SCCs). |
| `debundle gate cut <id>`                                                               | no       | Just the cut edges for one blocking SCC. The actionable subset — spec authors read this to pick which back-edge to break.                                                                                                                                                                                                                              |

The `gate` namespace names what is rejecting: the realizability
gate. It complements `scc` (which lists every quotient SCC,
including singletons and realizable multi-node ones); `gate` lists
**only** the SCCs the gate rejected. The unit is the blocking SCC,
not a cycle — a single SCC can contain exponentially many simple
cycles, so the CLI exposes the cut (a primitive on the SCC) but
deliberately not a `cycle list`.

Each `gate ...` command accepts an optional `--cycles <path>` flag
that overrides the default `cycles.json` location (sibling of
`--graph`). All other inputs follow the standard `--graph` /
`--modules` convention below.

### Atomic-DAG queries

These are top-level commands. Deprecated `debundle peel <...>` aliases
still exist for compatibility, but new docs and scripts should use the
top-level forms.

| Command                     | Mutates? | Function                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `debundle atoms`            | no       | List structural atoms (owner-level SCCs of the constraining-edge graph; per design.md §"Two classes of atom").                                                                                                                                                                                                                                                       |
| `debundle coverage`         | no       | Report spec coverage against atoms: which atoms are claimed, which fall through to residual. Add `--include-proposals` when rows should include matching factorizer proposal ids.                                                                                                                                                                                    |
| `debundle graph-summary`    | no       | High-level counts (owners, edges, atoms, residual-eligible bindings, etc.). Add `--include-proposals` when proposal and diagnostic counts are needed.                                                                                                                                                                                                                |
| `debundle describe <id>`    | no       | Dereference any identifier with full graph + spec context. Accepted ID kinds: a binding (minified `XOe` or readable `PluginSettingsAccessor`), a module path (`runtime/plugins`), a module id (`logical:7`), a proposal id, an atom id, an owner id (`owner:42`), a diagnostic id. Add `--include-proposals` when nearby proposal/diagnostic annotations are needed. |
| `debundle show-source <id>` | no       | Print the source text for any identifier. Accepted ID kinds: binding (minified or readable), module path or unambiguous module filename or module id (concatenated source of every owner statement in the module, in declaration order), proposal id, atom id, owner id, diagnostic id.                                                                              |

The `peel` namespace is deprecated. Existing `peel <...>` invocations
continue to work as aliases with a stderr deprecation note pointing to
the top-level form.

## Argument conventions

Three common paths show up on most commands. Each accepts both a
flag and an env var; the flag wins if both are set.

| Flag                  | Env var                | Meaning                                                                                                                                                                                                                         |
| --------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--graph <path>`      | `DEBUNDLE_GRAPH`       | `owner_graph.json` for the chunk being inspected. The graph path implies the chunk; multi-chunk callers point at different graphs per invocation.                                                                               |
| `--modules <dir>`     | `DEBUNDLE_MODULES`     | Per-module YAML tree root (the directory under `spec/modules/`).                                                                                                                                                                |
| `--source-root <dir>` | `DEBUNDLE_SOURCE_ROOT` | Upstream snapshot root containing the original chunk bytes. Needed by `show-source`, by `describe` for IDs that resolve to a source location, and by `modules propose` to annotate anonymous-statement selector addressability. |

Setting all three env vars in the shell once per session lets
commands run without repeating the flags:

```bash
export DEBUNDLE_GRAPH=$REPORTS/tree/static/index-EXAMPLE/owner_graph.json
export DEBUNDLE_MODULES=vendored/web/example-snapshot/spec/modules
export DEBUNDLE_SOURCE_ROOT=vendored/web/upstream/example-snapshot

debundle describe XOe
debundle scc --binding XOe
debundle bindings assign XOe:runtime/plugins
```

## Output format

Every read-only command supports `--format <text|json|ndjson>`:

- `text` — human-readable default for interactive use.
- `json` — single JSON document.
- `ndjson` — one JSON value per line, for streaming consumers (`jq -c`, piping to other commands).

If `--format` isn't passed and stdout is **not** a tty (i.e. the
command is in a pipeline), the default flips to `json`. So
`debundle modules propose | jq …` works without an explicit
`--format json`.

Read-only inspection commands prefer fast graph/spec lookups. `modules
propose` is the command that runs the proposal factorizer by default.
`describe`, `coverage`, and `graph-summary` do not run it unless the
selection itself is a proposal/diagnostic id or `--include-proposals`
is passed. Proposal-derived JSON fields are omitted when the factorizer
is skipped.

Mutating commands (`bindings assign`, `bindings unassign`,
`bindings rename`, `modules merge`, `modules delete`) print a
one-line "ok" / "would change N
files" / "rejected" result. Combined with `--dry-run` they
currently print only the verdict; a structured diff (post-mutation
YAML preview) is a documented TODO in the codebase but not in v1.

## Batch atomicity (`bindings assign`)

`bindings assign` takes one or more positional triples
`<sym>:<module>[:<readable>]`. Validation runs on the post-batch
spec, not after each individual assignment — so multi-binding
refactors whose intermediate (after some-but-not-all moves) states
would be invalid can land in one shot.

### Input shapes

```bash
# 1. Single move, keep current name.
debundle bindings assign --modules $MOD XOe:runtime/plugins

# 2. Single move + rename.
debundle bindings assign --modules $MOD XOe:runtime/plugins:PluginSettingsAccessor

# 3. Multi-move batch (positional triples).
debundle bindings assign --modules $MOD \
    XOe:runtime/plugins:PluginSettingsAccessor \
    YOe:runtime/plugins \
    ZOe:runtime/widgets:WidgetRegistry

# 4. Large refactors: --batch reads JSON from a file (or `-` for stdin).
debundle bindings assign --modules $MOD --batch moves.json
debundle bindings assign --modules $MOD --batch -  < moves.json
```

`<sym>` accepts either the minified name (`XOe`) or the current
readable name (`PluginSettingsAccessor`). The optional third field
sets the **new** readable name; omitting it preserves whatever
readable name is currently in the spec.

### `--batch` JSON format

A top-level JSON array of move objects:

```json
[
  { "sym": "XOe", "module": "runtime/plugins", "readable": "PluginSettingsAccessor" },
  { "sym": "YOe", "module": "runtime/plugins" },
  { "sym": "ZOe", "module": "runtime/widgets", "readable": "WidgetRegistry" }
]
```

- `sym` and `module` are required.
- `readable` is optional; omitting it preserves the binding's current readable name.
- Array order is preserved for dedupe semantics (last-wins when the same `sym` appears more than once).

`--batch` also accepts `modules propose --format json` output, or a
top-level array of proposal objects selected from `.proposals`, when
every selected proposal is a direct member move:

- `landable_today: true`
- non-empty `binding_ids`
- no `merge_into`
- no `anonymous_statement_owner_ids`

Fresh proposals move each `binding_ids[]` entry to
`proposed_module_id`; extension proposals move them to
`extends_module_id`. `merge_into` rows require `modules merge` or
manual YAML because they combine existing source modules. Rows with
`anonymous_statement_owner_ids` require `anonymous_statements:` edits,
which `bindings assign` does not perform.

JSON over TSV because it is schema-validated, queryable with `jq`, and
composable with reviewed JSON outputs from the same CLI without a TSV
conversion step.

### Atomicity contract

1. Parse all moves from positional + `--batch`. Dedupe; the _last_
   assignment for a binding wins (with a stderr warning on
   conflict).
2. Read the current spec. Compute the post-batch spec in memory.
3. Run the realizability gate on the post-batch spec.
4. If invalid (or any duplicate-binding-claim from the simulated
   moves would surface): print binding-pair blame, exit non-zero,
   **do not modify any file**.
5. If valid: write every affected YAML in one pass. The output is
   atomic from the consumer's perspective — every file that
   changes does so together.

`--dry-run` runs steps 1–4 and stops; reports the validation
result + the planned diff. `--no-verify` skips step 3 (still does
duplicate-claim detection — that's a structural error, not a
validation one).

### Per-move semantics

If you want refuse-intermediate-invalid semantics, invoke
`bindings assign` once per move. Per-batch is the default because
the common case for batch is "this refactor needs all-or-nothing
application." Per-move would be the surprising default.

## Rejection diagnostics

When validation refuses a mutating command, the diagnostic names
exactly what's wrong without making the spec author re-derive the
analysis from scratch. Two kinds:

**Atom split** (refused by the realizability gate). The diagnostic
lists each split atom: which owners it covers, which modules its
members would land in, and the `DepKind` causes from the unit
(eager cycle, rebind, sequenced, etc. — same data shape as
`AtomicUnitConflict`). Example shape:

```
atom-split refused — cannot apply 3 of 5 moves:
  atom { iRe, rRe, MRe } [causes: eager_use]
    iRe -> domains/system/ids       (planned by request)
    rRe -> domains/system/schemas   (CURRENT — unmoved)
    MRe -> domains/system/schemas   (CURRENT — unmoved)
  reason: rRe and MRe must co-locate with iRe; the requested move
    splits the atom into ids vs schemas.
```

The diagnostic does **not** auto-compute the minimal completion
("also move rRe and MRe to ids") — that's deferred (see "Out of
scope" below). It does name the owners and the destinations, so
the author can compute the completion by inspection.

**Name collision** (refused by `bindings rename` or by `bindings
assign` when a `:readable` field collides). Lists each collision:
the existing binding holding the name, the binding the rename
would have given it.

Both diagnostic shapes go to stderr; the command exits non-zero.
With `--format json` the diagnostic is also serialized to stdout
as a structured object so machine readers can parse it.

## Out of scope

- **No cross-process materializer reader.** `debundle run` reads
  the spec and emits JS in one process. There is no
  `materialize-from-cache` mode — see `wire_format.md` §"Why
  pre-filter facts (`StatementFacts`) aren't on the wire" and
  `lessons_learned/cross_process_stage_b.md` for the analysis.
- **`facts.json` is not a CLI input.** It's an in-process debug
  artifact at `reports/tree/<chunk>/chunk_analysis/facts.json`. See
  `facts/wire.rs` module docstring.
- **Module rename / disable** is just `mv` on the YAML file (see
  "Modules" above). No dedicated subcommand.
- **Auto-computed minimal completion** for atom-split rejections.
  The diagnostic names the split atom and which destinations its
  members would land in (see "Rejection diagnostics") but does not
  compute the smallest extra-moves set that'd fix it. The author
  reads off the completion from the printed atom membership.
  Worth revisiting once the basic CLI surface is in use.
- **YAML diff in `--dry-run`.** v1 prints only an "ok / would
  change N files" verdict line. A structured diff (post-mutation
  YAML preview) is a documented TODO in the codebase but not in
  v1.
- **Tab completion.** Not in v1.

## Comments

The spec carries reverse-engineering annotations as `comment:`
fields on members, modules, and anonymous statements. Those comments
live in YAML and emit into generated JS on every rebuild, so RE notes
survive `debundle run` invocations.

`anonymous_statements:` entries may carry `comment:` or `note:`. They
are preserved on round-trip. Anonymous statement comments are not
edited by a dedicated CLI command. `comment:` emits before the matched
anonymous statement; `note:` does not emit.

Debundle's output is meant to turn minified compiled chunks into nice
human-readable code. Use emitted `comment:` text as part of that
readability surface: explain intent, invariants, and module
relationships. Keep provenance, owner IDs, and source-call trivia in
`note:` or omit them. Use `note:` for scratch reverse-engineering
notes that should survive debundle edits but should not appear in
generated JS, including uncertainty, provenance, and call-site
observations.

Current state:

- **CLI editing**: `debundle bindings comment <sym>`
  and `debundle modules comment <module>` set / read / `--edit` /
  `--clear` the field.
- **Spec + lowering**: module comments emit at the top of the
  generated module file; member comments emit immediately above the
  binding's owner statement; anonymous-statement comments emit
  immediately above the matched statement. Empty comments emit
  nothing.

Move semantics (these are properties of the existing CLI surface,
not separate features):

- `bindings assign` carries member comments with the member as it
  moves between modules. The comment is part of the member entry;
  the move is a YAML splice, so it round-trips.
- `bindings assign` auto-deletes a drained source module only when
  its module-level `comment:` is empty/absent. A module with a
  module-level doc but no remaining members is kept as a
  `members: []` shell.
- `modules merge` concatenates source-module comments into the
  target's module-level `comment:` (with a `--- from <source>:`
  divider) when sources have non-empty comments.

See `guide.md` → "Workflow: authoring `comment:` fields" for the
YAML schema and worked CLI examples.

## See also

- `AGENTS.md` — generic operator workflows that compose these
  commands.
- `design.md` — the realizability theorem the validation gate
  enforces.
- `wire_format.md` — JSON sidecar conventions readers of these
  commands consume.
- `lessons_learned/cross_process_stage_b.md` — why pre-filter
  analyzer facts are not a cross-process CLI input.
- `design.md` §"Layered mental model" + §"Factor assembly inside
  `debundle run`" — the factorization algorithm `modules propose`
  draws its proposals from.
