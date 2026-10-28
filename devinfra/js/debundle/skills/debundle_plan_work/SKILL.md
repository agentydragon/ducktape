---
name: debundle_plan_work
description: Plan JS debundle extraction or existing-module reorganization using read-only graph and source queries. Use for residual proposals, gate-checked decomposition, atomic coverage, selector anchors, and reviewed source-unit assignment handoffs. Generic to any debundle target; this skill does not edit the spec.
---

# Debundle Plan Work

Plan from a current graph, its matching source/spec tree, project conventions,
and emitted reports. This role is read-only: produce evidence for an edit rather
than mutating YAML or applying a batch.

Read [plan an assignment](references/docs/planning.md#plan-an-assignment), then
use the [task chooser](references/README.md#choose-a-task) for the relevant
inspection, residual, decomposition, or selector recipe. The planning guide owns
query selection, atomic coverage, selector anchors, and the distinction between
heuristic proposals and source-backed facts.

Return scoped units/proposals, semantic destinations, source evidence, candidate
verdicts, input identity, and unresolved questions in the form described there.
Send residual proposals to intake; reviewed existing-module maps go directly to
a worker. Report repeated synthesis gaps as tooling work. If Debundle blocks the
task, follow the [bug reproduction workflow](references/docs/bug_reproductions.md).
