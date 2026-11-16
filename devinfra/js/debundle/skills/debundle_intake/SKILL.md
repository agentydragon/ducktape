---
name: debundle_intake
description: Turn a reviewed exact residual placement into a named worker handoff. Use for source-grounding and semantic module naming while preserving the checked assignment plan; do not edit the spec or run gates.
---

# Debundle Intake

Prepare one residual-placement handoff from the exact search report, matching
graph/modules/source, and project conventions. Intake does not edit YAML, run
gate/regeneration commands, or integrate branches.

Read [prepare a residual-placement handoff](references/docs/planning.md#prepare-a-residual-placement-handoff)
for source grounding and plan-preservation rules. Use the
[task chooser](references/README.md#choose-a-task) for source inspection. The
snapshot-bound plan is one jointly checked assignment: keep every change and
destination together. A graph-ready plan still needs the worker's selector
proof, edit gate, materialization and runtime checks.

Review source behavior and callers to describe the intended boundaries and
name candidate modules. Preserve the captured destinations and attach semantic
and source evidence separately. Route destination edits or combined batches
back to planning for a complete check. `incomplete` does not prove any residual
code blocked.

Return the intact plan, named module descriptions, source evidence and
architecture questions. Keep source reading focused; route destination
decisions needing broad architectural discovery to the architect. If Debundle
blocks the task, follow the [bug reproduction workflow](references/docs/bug_reproductions.md).
