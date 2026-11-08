# Debundle Tool Friction and Bug Reproductions

## While using Debundle

Use the Debundle CLI and its reports as the working tools for spec authoring and
code investigation. If a query, selector, extraction, or validation command is
awkward or fails, make a small, obvious recovery attempt when appropriate.
Report minor UX friction or small bugs and the workaround in the task result.

Report a substantive toolkit bug or missing capability as soon as it is clear,
or when working around it would take roughly ten tool calls or start requiring
Debundle implementation debugging. A scoped lane worker returns the blocker
to the orchestrator instead of silently expanding its assignment. The
orchestrator keeps the spec objective active and chooses among
[general semantics, sound specific recognition and an audited annotation](../skills/shared/workflow.md#choose-the-solution-depth),
then routes authorized work. Do not quietly replace Debundle with a custom
extractor, graph parser, or source rewrite that belongs in the toolkit. Do not
treat output from a failed or bypassed gate as verified.

Give the failing command or operation, expected and observed behavior, the
smallest known input shape, and any exact diagnostic needed to pick up the bug.
Keep private source out of public reports. Resume the original task after the
tooling issue is handled.

## When fixing a Debundle bug

1. Localize the failure: input shape, pipeline stage, relevant file and lines,
   and the observable wrong result.
2. Reduce it to the smallest synthetic end-to-end fixture under `e2e/` that
   fails for the same reason through the real `debundle` CLI. Show that it
   fails before the fix; a fixture that already passes on the base revision is
   not a regression test for this bug.
3. Choose the solution depth for the reduced case. A correct sufficient-condition
   recognizer or explicit contract can address the underlying limitation without
   general interpretation. Keep the selected proof or assertion visible, show
   the fixture turns green and land it with the change. Acceptance of the
   synthetic case still needs downstream evidence for the original blocker.

Write new fixture code with neutral names and shapes such as `a`, `b`,
`foo`, `bar`, `interface`, `class`, `provider`, or `factory`. Strip every
removable feature. Never commit verbatim snippets, distinctive identifiers,
strings, or other source-specific material from a private debundling target to
Ducktape. Keep each fixture focused on one bug class and assert the external
contract: emitted exports, source shape, file layout, or runtime behavior.

If the private failure cannot genuinely be reproduced with a synthetic
fixture, explain that limit in the PR, add coverage at the next useful level,
and smoke-test the private corpus.

For a proof-coverage regression, assert the exact source owner's operation
receipt and the effect assessment alongside the actual executable inversion
and runtime result. An incomplete effect assessment can coexist with an
accepted ordinary gate and emitted inversions: the effect report is currently
report-only. A negative control should verify the missing receipt or uncovered
effect, rather than assume that it prevents every reorder.

## Downstream acceptance and stricter checks

Before changing acceptance, compare the candidate and a working engine against
the same downstream source/spec pair. Record successful, rejected, and unverified
controls separately with their source, spec, and tool revisions. Classify a new
rejection as a spec conflict, an analysis defect, or unsupported reconstruction;
land the diagnostics and repair capabilities before enabling stricter checks.

Allow at most two hours of focused repair for a downstream conflict, stopping
sooner when a deep redesign becomes clear. If repair exceeds that budget, defer
the stricter change with its synthetic reproduction, downstream impact, and
remaining work. Continue independent refactoring. Keep the downstream repository
on its working pin until the candidate engine and repaired specs validate
together. Never weaken validation or change rule exemptions or baselines to
make a mechanical cutover appear green.
