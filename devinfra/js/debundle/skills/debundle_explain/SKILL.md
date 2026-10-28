---
name: debundle_explain
description: >-
  Answer questions about how software works when its repository has a full or
  partial debundle spec, extending the spec and emitted modules to cover the
  code needed to explain the behavior. Use by default for such questions even
  when the user does not say "debundle"; always use it when they do.
---

# Debundle Explain

Answer a software-behavior question from its source and debundled modules.
Find the target's spec, captured source, adapter, and tests. If the source/spec
is unavailable, state the evidence limit and answer from what is available.

Read [explain behavior with a partial spec](references/docs/cli.md#explain-behavior-with-a-partial-spec).
Use the [task chooser](references/README.md#choose-a-task) for inspection,
extraction, naming, and selector recipes. Extend coverage only for code required
by the question; keep unrelated cleanup out of scope and preserve upstream source.

Return the observed control/data flow, generated paths and functions that show
it, spec additions, exact gate/runtime results, and remaining uncertainty.
Substantial discovery, architecture, or multi-lane extraction should go to the
corresponding specialist roles in the
[shared workflow](references/skills/shared/workflow.md#roles).
If tooling blocks the task, follow the
[bug reproduction workflow](references/docs/bug_reproductions.md).
