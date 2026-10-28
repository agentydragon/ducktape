---
name: debundle_orchestrator
description: Coordinate a generic AI-driven debundling loop across intake, lane worker, architect, integrator, planning, and naming skills. Use for multi-agent debundle rounds, work routing, graph refreshes, gate/regen command broadcast, progress tracking, and adapter-specific workflow control.
---

# Debundle Orchestrator

Coordinate specialist roles and own project-adapter inputs, scope, commands,
and progress tracking. Keep source discovery, architecture decisions, naming,
and merge trains with their respective roles as that work becomes substantial.

Read [coordinate a round](references/skills/shared/workflow.md#coordinate-a-round)
and the [adapter contract](references/skills/shared/workflow.md#adapter-contract).
The workflow's phase table routes planning, intake, worker execution, and
integration; the [task chooser](references/README.md#choose-a-task) routes
individual authoring operations.

Dispatch each writer with isolated complete inputs and explicit scope. Collect
relative YAML deltas and completed execution evidence, then require validation
of the combined result. The shared workflow owns snapshot identity, proof
boundaries, status tracking, and failure routing.

If Debundle tooling blocks a lane, follow the
[bug reproduction workflow](references/docs/bug_reproductions.md).
