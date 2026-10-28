---
name: debundle_lane_worker
description: Apply one scoped debundle extraction, source-unit assignment refinement, or reorganization task in a worktree. Use for reviewing source and atomic coverage, choosing module boundaries, applying validated spec edits, running adapter gates and regeneration, and committing one reviewable worker branch.
---

# Debundle Lane Worker

Apply one scoped extraction, assignment refinement, or reorganization task in
an isolated worker snapshot. The adapter supplies the permitted YAML subtree,
expected base, copied inputs/engine, and checks. Preserve upstream source and
regenerate emitted output through the adapter.

Read [execute a delegated assignment](references/docs/spec_editing.md#workflow-executing-a-delegated-assignment)
and its linked [adapter contract](references/skills/shared/workflow.md#adapter-contract).
Choose the operation-specific recipe from the [task chooser](references/README.md#choose-a-task).
Those guides own input compatibility, atomic coverage, dry-run review, selector
proof, graph refresh, and validation order.

Return one reviewable relative YAML delta and the
[execution evidence](references/skills/shared/workflow.md#execution-evidence),
including actual check results and skipped work. The commit transports the edit;
it does not replace snapshot validation. Route unclear architecture to the
architect, broad source discovery to intake, and unsupported tooling through the
[bug reproduction workflow](references/docs/bug_reproductions.md).
