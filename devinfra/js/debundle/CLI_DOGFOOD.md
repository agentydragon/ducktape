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

## Planner CLI follow-ups

Generic usability follow-ups for the top-level planner commands.
Corpus-specific paths and owner ids belong in the consuming repo.

- **Selector synthesis filters apply too late.** A downstream large-spec
  dogfood run of `debundle spec synthesize-selectors --rewrite
name-binding-to-source-match` showed that even one explicit
  `--item module:export` scanned every module file and member in the spec:
  `files_scanned=1745`, `modules_scanned=1745`, `members_scanned=6692`,
  `name_binding_members=1`, `elapsed=3.43s`. Scoped `--module-prefix` dry runs
  timed out at 30s CPU-bound. Explicit item batches were useful but still paid
  full-scan cost: top-100 items took 16.37s for 75 candidate changes; top-200
  took 31.38s for 157 candidate changes. Apply item/file/module filters before
  full YAML traversal and before source candidate generation where possible.
- **Selector synthesis apply emits non-reviewable YAML churn.** The same
  downstream dogfood run applied a top-100 item batch with 75 changed
  candidates. Selector correctness looked promising, but the YAML application
  path rewrote unrelated text: 13 files changed with 7331 insertions and 4273
  deletions, one large module accounted for most churn, and an unrelated
  top-level comment was dropped. Source-aware selector synthesis needs a
  text-preserving patch path for member selector replacement and
  binding-group/member collapse before broad generated patches are reviewable.
- **Selector synthesis needs a minimization acceptance loop.** A generated
  selector that exactly copies today's large function body, object literal,
  argument list, or class body can match uniquely while still being fragile
  spec debt. Dry-run/apply output should surface when a
  candidate is long/exact and should either minimize it automatically with
  `ANYTHING`, typed holes, `OBJECT_PROPS`, `CLASS_REST`, `STMT_LIST`, or
  `DECLARATORS`, or emit a stable tooling-gap diagnostic that agents can route
  instead of hand-maintaining the exact body.
- **Orthogonal patch-plan workflow.** New spec automation should converge on
  the inventory/plan/apply/validate/explain model in
  <plans/automated_spec_workflows.md>. A dry-run that proposes edits should be
  able to emit a stable plan artifact; apply should consume that artifact; repair
  should consume validate diagnostics. Avoid adding isolated flags whose output
  cannot feed the next command.
- **Diagnostics toggle for `modules propose`.** `--limit` now bounds
  proposals and diagnostics and the `limits` summary reports totals
  when details are truncated, but there is still no explicit
  diagnostics on/off toggle for first-pass planning, where proposal
  rows are the only thing the caller wants.
- **Concise explain mode.** Proposal/diagnostic structures on
  `describe` are already opt-in (`--include-proposals`), but there is
  still no compact mode focused on: selected owner identity and source
  span, atomic-unit membership, matching proposal (if any), immediate
  constraining neighbors, and the exact reason the owner is not
  landable today.
- **Source roots.** `source-slice --source-root ...` depends on the
  consuming target's source-tree layout. Runbooks and skills should make
  that target-specific root explicit instead of assuming repository root
  or working directory.
- **Patch-plan naming.** `coverage` is useful for intersecting existing
  module YAML with atomic-unit coverage, but it is not the only way to
  discover readable work: `atoms --readable-only` and `modules propose`
  may show graph-valid work even when no whole patch section is ready.
  Docs and skill text should reserve "plan" for proposed edits that can be
  reviewed/applied, and avoid implying that empty coverage output means there
  is no landable work.
