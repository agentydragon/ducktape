---
name: debundle_plan_work
description: Plan debundle extraction and reorganization with read-only graph/source queries. Use for atomic residual splits, gate-driven SCC coarsening, passing residual baselines and divide-and-conquer probes, residual proposals, selector anchors, and reviewed source-unit handoffs. Explains what the current analysis permits separating; does not edit the spec.
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

To inspect separability, compare
[atomic decomposition including residuals](references/docs/cli.md#refining-existing-modules-modules-decompose)
(`modules decompose --atomic --include-residual`) with a candidate using
`--coarsen-blocking-sccs`. Review structural atoms, gate-reported merges, and the
complete candidate verdict. These describe the current model and assumptions,
not inherently indivisible JavaScript or semantic module boundaries. For an
emitted-output probe, hand a worker the
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
