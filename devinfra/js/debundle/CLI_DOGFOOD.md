# Debundle CLI Dogfood Backlog

Current open CLI usability and scripting-safety findings from exercising
the documented workflows against a real spec. Resolved items are deleted;
this file is not a changelog.

Each item: severity, command, expected behavior, observed behavior, and a
fix idea where one is obvious.

## 🟡 Confusing (UX, not soundness)

### 1. `cluster --binding <sym>` documented but rejected

`tana/re/web/AGENTS.md` shows `$BIN cluster --binding XOe --format
ndjson`. The CLI actually wants a positional `<SYM>`: `error: unexpected
argument '--binding' found`.

**Fix idea**: either drop the `--binding` flag form from AGENTS.md or
add it as an alias in the CLI parser.

### 2. `cluster` output uses opaque `logical:N` ids without labels

`debundle cluster XOe` returns:

```json
"home_module": "logical:2009",
"outgoing_modules": ["logical:1031", "logical:1046", ...]
```

`describe` happily prints labels like `static/index-DI2GynTv::app/locale/locale_settings`.

**Fix idea**: include `"label"` / `"path"` alongside the `logical:N` id
in cluster output, matching describe's shape.

### 3. `modules delete` requires `.yaml` suffix; the error message hides it

`debundle modules delete --dry-run auto_partition/auto_partition_0004`
errors with `module path does not exist:
…/spec/modules/auto_partition/auto_partition_0004`. Add `.yaml` and it
works.

`modules comment` and `bindings assign` both accept the bare module
path; only `modules delete` requires the suffix. Inconsistent.

**Fix idea**: accept the bare path (consistent with siblings) or change
the error to "expected `.yaml` suffix".

### 4. `gate list` silent when `cycles.json` missing

`debundle gate list` with no current cycles emits a single `reading
…/cycles.json` to stderr and exits 0 (no body). Indistinguishable from
"file missing" vs "no rejections".

**Fix idea**: emit `[]` (json) or `no blocking SCCs` (text). When the
file is missing, error explicitly.

## 🔵 Minor doc inconsistencies

### 5. `tana/re/web/AGENTS.md` BIN path stale

The doc says `BIN=bazel-bin/external/ducktape_debundle_bin/file/debundle`.
The actual path now has a `+_repo_rules+` prefix:
`bazel-bin/external/+_repo_rules+ducktape_debundle_bin/file/debundle`.

**Fix**: update gaffer-private's AGENTS.md.

### 6. `describe` text format missing home-module path

JSON output includes `binding_homes[].path`. Text output shows owners,
bindings, atom membership, edge counts — but no module path. Either the
text output should include the path, or the docs should reflect text's
narrower surface.

### 7. `bindings comment` read with empty comment returns empty string

Reading an unset comment returns `{"sym": "...", "comment": "",
"action": "read"}`. Indistinguishable from an explicit `comment: ""` in
the spec. Docs say "empty if none."

**Fix idea**: return `"comment": null` or omit the field when unset.

### 8. `describe <sym>` text format hangs on repeat invocations

First invocation returned a 5-line summary; second invocation of the
same command hung indefinitely. `--format json` consistently completes
in ~30s. May indicate a stale cache or non-idempotent text renderer.

## `bindings` / `modules` gaps

Small `bindings ...` / `modules ...` gaps found while surveying real
specs; nothing structural.

- **`modules list --empty` shows every empty module, including ones
  preserved by a `comment:`.** The actually-actionable subset is
  "empty AND no comment" — i.e. the auto-deletable set. Add a
  `--auto-deletable` filter (or expose `--empty --no-comment`) so
  `debundle modules list --auto-deletable --format json | jq -r '.modules[].path' | xargs rm`
  is the safe one-liner for sweeping drained cruft.
- **`modules list` member-count is the only signal of module size**;
  there's no quick way to spot a module whose member count is right but
  whose `anonymous_statements:` count is huge (the residual case). An
  optional `--with-anonymous` flag exposing that count alongside
  `member_count` would let `debundle modules list --residual --with-anonymous`
  surface the residual sentinel's anonymous-statement drift over time.

## Planner CLI follow-ups

Generic usability follow-ups for the top-level planner commands.
Corpus-specific paths and owner ids belong in the consuming repo.

- **Bounded planner output.** `debundle modules propose` is the main
  dispatch surface for agents and humans; for fresh or sparse specs,
  output can become hard to consume when proposal details and
  diagnostics are both large. Keep proposal output bounded by default
  when `--limit` is supplied, expose summary counts even when details
  are truncated, consider an explicit diagnostics toggle for first-pass
  planning, and keep sort keys documented and stable.
- **Concise explain mode.** `debundle describe --owner-id ...` should
  have a compact mode focused on: selected owner identity and source
  span, atomic-unit membership, matching `plan-work` proposal (if any),
  immediate constraining neighbors, and the exact reason the owner is
  not landable today. Large proposal/diagnostic structures should be
  opt-in when the caller is debugging the planner itself.
- **Source roots.** `source-slice --source-root ...` depends on the
  consuming target's source-tree layout. Runbooks and skills should make
  that target-specific root explicit instead of assuming repository root
  or working directory.
- **Patch-plan naming.** `coverage` is useful for intersecting existing
  module YAML with atomic-unit coverage, but it is not the only way to
  discover readable work: `atoms --readable-only` and `modules propose`
  may show graph-valid work even when no whole patch section is ready.
  Docs and skill text should avoid implying that empty coverage output
  means there is no landable work.
