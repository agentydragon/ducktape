# Debundle CLI Dogfood Backlog

Current open CLI usability and scripting-safety findings from exercising
the documented workflows against a real spec. Resolved items are deleted;
this file is not a changelog.

Each item: severity, command, expected behavior, observed behavior, and a
fix idea where one is obvious.

## 🔵 Minor doc inconsistencies

### 1. `tana/re/web/AGENTS.md` BIN path stale

The doc says `BIN=bazel-bin/external/ducktape_debundle_bin/file/debundle`.
The actual path now has a `+_repo_rules+` prefix:
`bazel-bin/external/+_repo_rules+ducktape_debundle_bin/file/debundle`.

**Fix**: update gaffer-private's AGENTS.md.

### 2. `describe` text format missing home-module path

JSON output includes `binding_homes[].path`. Text output shows owners,
bindings, atom membership, edge counts — but no module path. Either the
text output should include the path, or the docs should reflect text's
narrower surface.

### 3. `describe <sym>` text format hangs on repeat invocations

First invocation returned a 5-line summary; second invocation of the
same command hung indefinitely. `--format json` consistently completes
in ~30s. May indicate a stale cache or non-idempotent text renderer.

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
