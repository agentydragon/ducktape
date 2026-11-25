---
name: debundle_plan_work
description: Plan debundle extraction and reorganization with read-only graph/source queries. Use for exact residual placement discovery, checked assignment plans, passing residual baselines, selector anchors and reviewed source-unit handoffs. Explains what the current analysis permits separating; does not edit the spec.
---

# Debundle Plan Work

Use the current graph, matching source/spec tree and project conventions to
[plan an assignment](references/docs/planning.md#plan-an-assignment). Planning
is read-only. Return the input identity, checked plan, source evidence,
destinations, selector anchors and unresolved questions.

- For an accepted partial spec, start with unseeded
  [residual placement](references/docs/module_proposals.md). Keep each chunk's
  outcome and the scope of a completed negative. Send the whole plan to intake
  for semantic review, or directly to a worker if the destinations are reviewed.
- For a proposed move, check the complete [destination batch](references/docs/cli.md#checking-explicit-destination-batches-modules-check).
  A worker needs an executable plan. Recheck changed destinations or combined
  batches before handing them off.
- For a broadly rejecting spec, plan [passing-baseline probes](references/skills/shared/workflow.md#start-from-passing-residual-output-and-isolate-failing-assignments).
  A worker can also probe [atomic-group residual policies](references/SPEC.md#unassigned-source)
  through normal emission.

Use the [task chooser](references/README.md#choose-a-task) for inspection and
selector work. Route repeated synthesis gaps through the
[bug reproduction workflow](references/docs/bug_reproductions.md).
