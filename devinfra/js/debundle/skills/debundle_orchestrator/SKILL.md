---
name: debundle_orchestrator
description: Coordinate a generic AI-driven debundling loop across intake, lane worker, architect, integrator, planning, and naming skills. Use for multi-agent debundle rounds, work routing, graph refreshes, gate/regen command broadcast, progress tracking, and adapter-specific workflow control.
---

# Debundle Orchestrator

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

Use this role to keep an AI-driven debundling loop moving. The orchestrator
routes work between specialist roles and owns project-adapter details.

Shared CLI workflows land here so planner, intake, and worker routing use the
same command semantics:

@references/docs/cli.md
@references/docs/spec_editing.md

Read other bundled references as needed:

- `references/workflow.md` for the adapter contract, round loop, state, and failure routing
- `references/README.md` for the crate overview
- `references/docs/bazel_integration.md` for the `debundle_pipeline` rule and profiling targets
- `references/module_shape.md` for when to route to architect or lane workers
- `references/docs/selectors.md` for the bar selector-stabilization work must meet

## Role Routing

- Use `debundle_plan_work` to refresh planner evidence.
- Send `modules propose` output to `debundle_intake` for seeds.
- For existing-module reorganization, ask the planner for a source-backed
  `modules decompose` candidate, including selector anchors and optional
  coarsening/rejoining as needed. Route the reviewed semantic owner map
  directly to a lane worker for `modules refine`; intake consumes proposals,
  not decomposition reports.
- For selector-stabilization rounds, start with
  `debundle spec selector-debt --group-module-depth N --format json` and
  dispatch broad, coherent buckets to lane workers. Prefer buckets that can be
  handled by `debundle spec synthesize-selectors` over hand-authored YAML; the
  bar their output must meet is `references/docs/selectors.md` § The contract and
  the ladder.
- Send seed clusters or reorg tasks to `debundle_lane_worker`.
- Wake `debundle_architect` periodically or when module shape seems to drift.
- Use `debundle_integrator` for merge trains of worker commits.
- Use `debundle_mint_names` for naming-only passes.

Do not absorb specialist work when it becomes substantial. If you are reading
many source bodies, dispatch intake. If you are redesigning module shape,
dispatch the architect. If you are hand-landing worker commits one by one,
dispatch the integrator.
