---
name: debundle_plan_work
description: Plan debundle extraction and reorganization with read-only graph/source queries. Use for exact residual placement discovery, checked assignment plans, passing residual baselines, selector anchors and reviewed source-unit handoffs. Explains what the current analysis permits separating; does not edit the spec.
---

# Debundle Plan Work

Plan from a current graph, its matching source/spec tree, project conventions,
and emitted reports. This role is read-only: produce evidence for an edit rather
than mutating YAML or applying a batch.

Read [plan an assignment](references/docs/planning.md#plan-an-assignment), then
use the [task chooser](references/README.md#choose-a-task) for the relevant
inspection, residual or selector recipe. The planning guide owns query selection,
atomic coverage, selector anchors and evidence requirements.

Use `modules check --batch` to validate a reviewed explicit destination batch
against the complete accepted assignment, including residual units and anonymous
statements. Keep omitted units in place and check the actual destination. A
successful check is graph evidence; selector realization and emission are separate.
Recheck the union of independently accepted batches before recommending it.

For "can anything still be peeled?", begin with unseeded
[exact residual placement](references/docs/module_proposals.md) using `modules propose`.
Use `--graphs` with the configured module tree for corpus discovery, or `--graph`
for one chunk. Inspect the discovered groups in matching source. Review the complete ownership map and
destinations, check and preview/apply the same snapshot-bound plan, then
regenerate and run behavior checks. Executable plans include the final import
priority and selector-writer preflight; graph-only inventory is identified as
such. Runtime evidence remains separate. A completed negative is limited to the
append-one-owner, baseline-preserving entry-residual family; unsupported routing
and interruption are inconclusive. Keep each chunk-local outcome visible in corpus results. Recheck a plan
after editing its destinations or combining it with another batch.

For a structural inventory, use `atoms`, `coverage` and `describe`. Required
co-location does not imply independent extractability or semantic ownership.
For an emitted-output probe, hand a worker the
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
Send whole residual plans to intake for semantic grounding; already reviewed
plans go directly to a worker. Preserve companion moves as one packet. Report repeated synthesis gaps as tooling work. If Debundle blocks the
task, follow the [bug reproduction workflow](references/docs/bug_reproductions.md).
