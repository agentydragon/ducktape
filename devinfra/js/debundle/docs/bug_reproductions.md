# Debundle Tool Friction and Bug Reproductions

## While using Debundle

Use the Debundle CLI and its reports as the working tools for spec authoring and
code investigation. If a query, selector, extraction, or validation command is
awkward or fails, make a small, obvious recovery attempt when appropriate.
Report minor UX friction or small bugs and the workaround in the task result.

Pause the spec or investigation task and report a substantive toolkit bug or
missing capability. Do this as soon as the problem is clear, or when working
around it would take roughly ten tool calls or start requiring Debundle
implementation debugging. Do not quietly replace Debundle with a custom
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
3. Reconsider the design if the reduced case exposes a missing capability or
   an unsound assumption. Fix the underlying behavior, then show the fixture
   turns green and land the fixture with the fix.

Write new fixture code with neutral names and shapes such as `a`, `b`,
`foo`, `bar`, `interface`, `class`, `provider`, or `factory`. Strip every
removable feature. Never commit verbatim snippets, distinctive identifiers,
strings, or other source-specific material from a private debundling target to
Ducktape. Keep each fixture focused on one bug class and assert the external
contract: emitted exports, source shape, file layout, or runtime behavior.

If the private failure cannot genuinely be reproduced with a synthetic
fixture, explain that limit in the PR, add coverage at the next useful level,
and smoke-test the private corpus.
