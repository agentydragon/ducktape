---
name: debundle_intake
description: Turn `debundle modules propose` output into named, dispatchable seed clusters for debundle lane workers. Use for reading factorizer proposals, lightly grounding source meaning, choosing tentative destinations, and producing seeds.json without editing the spec or running gates.
---

# Debundle Intake

Turn residual planner proposals into named seed packets. Inputs are the
proposal JSON, matching graph/modules/source, project conventions, and a scratch
directory. Intake does not edit YAML, run gate/regeneration commands, or
integrate branches.

Read [prepare seeds](references/docs/planning.md#prepare-seeds) for readiness,
dependency-closure handling, source-grounding scope, ordering, and the
`seeds.json` format. Use the [task chooser](references/README.md#choose-a-task)
for source inspection. A graph-ready proposal still needs the worker's selector
proof and edit gate.

Return named packets, blocked/ambiguous proposals, and architecture questions.
Keep source reading focused; route destination decisions needing broad
architectural discovery to the architect. If Debundle blocks the task, follow
the [bug reproduction workflow](references/docs/bug_reproductions.md).
