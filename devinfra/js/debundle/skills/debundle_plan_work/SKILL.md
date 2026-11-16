---
name: debundle_plan_work
description: Plan debundle extraction and reorganization with read-only graph/source queries. Use for atomic-group inventories, passing residual baselines and divide-and-conquer probes, residual proposals, selector anchors, and reviewed source-unit handoffs. Explains what the current analysis permits separating; does not edit the spec.
---

# Debundle Plan Work

Plan from a current graph, its matching source/spec tree, project conventions,
and emitted reports. This role is read-only: produce evidence for an edit rather
than mutating YAML or applying a batch.

Read [plan an assignment](references/docs/planning.md#plan-an-assignment), then
use the [task chooser](references/README.md#choose-a-task) for the relevant
inspection, residual, assignment, or selector recipe. The planning guide owns
query selection, atomic coverage, selector anchors, and the distinction between
heuristic proposals and source-backed facts.

Use `modules check --batch` to validate a reviewed explicit destination batch
against the complete accepted assignment, including residual units and anonymous
statements. Keep omitted units in place and check the actual destination. A
successful check is graph evidence; selector realization and emission are separate.
Recheck the union of independently accepted batches before recommending it.

For "can anything still be peeled?", inspect the current graph and source, then
use [exact residual placement](references/docs/module_search.md) with
`modules propose` (exact by default). Review the complete ownership map and
destinations, check and preview/apply the same snapshot-bound plan, then
regenerate and run behavior checks. `found` establishes graph acceptance only;
selector realization and runtime behavior are separate. `exhausted_no_peel` is
limited to the recorded owner and destination scope. Budget exhaustion is
incomplete, and empty advisory output is not a negative decision. Recheck a plan
after editing its destinations or combining it with another batch.

For a structural inventory, inspect the atomic groups in the graph report. Use
`modules check --batch` to validate an authored assignment; structural groups
describe graph constraints, not semantic module boundaries. For an emitted-output
probe, hand a worker the
[`per_atomic_group` or `coarse_atomic_groups` residual policy](references/SPEC.md#unassigned-source);
authored claims remain and the normal gate still applies.

For a broadly rejecting spec, plan a
[residual-only baseline and divide-and-conquer probes](references/skills/shared/workflow.md#start-from-passing-residual-output-and-isolate-failing-assignments).
Identify a useful passing subset and a small conflicting addition. Preserve
assertions and input identity, and distinguish assignment rejection from an
invalid probe. This read-only role returns the probe plan and evidence to a
worker; it does not remove canonical claims.

Return scoped units/proposals, semantic destinations, source evidence, candidate
verdicts, input identity, and unresolved questions in the form described there.
Send residual proposals to intake; reviewed existing-module maps go directly to
a worker. Report repeated synthesis gaps as tooling work. If Debundle blocks the
task, follow the [bug reproduction workflow](references/docs/bug_reproductions.md).
