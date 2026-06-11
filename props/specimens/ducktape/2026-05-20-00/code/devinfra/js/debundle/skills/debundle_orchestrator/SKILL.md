---
name: debundle_orchestrator
description: Coordinate a generic AI-driven debundling loop across intake, lane worker, architect, integrator, planning, and naming skills. Use for multi-agent debundle rounds, work routing, graph refreshes, gate/regen command broadcast, progress tracking, and adapter-specific workflow control.
---

# Debundle Orchestrator

Use this role to keep an AI-driven debundling loop moving. The orchestrator
routes work between specialist roles and owns project-adapter details.

Read bundled references as needed:

- `references/workflow.md` for the shared multi-agent workflow
- `references/debundle_user_guide.md` for common CLI evidence surfaces
- `references/module_shape.md` for when to route to architect or lane workers

## Adapter Contract

Before dispatching work, collect:

- `<debundle-target>` and graph-refresh command
- `<graph>`, `<modules-dir>`, `<emitted-js-root>`, source root if available
- gate, regen, uniqueness-check, and optional smoke-test commands
- project conventions/taxonomy docs
- architecture notes and reorg recommendation paths
- worktree policy, base branch, commit/push policy, and scratch paths

## Role Routing

- Use `debundle_plan_work` to refresh planner evidence.
- Send `plan-work` output to `debundle_intake` for seeds.
- Send seed clusters or reorg tasks to `debundle_lane_worker`.
- Wake `debundle_architect` periodically or when module shape seems to drift.
- Use `debundle_integrator` for merge trains of worker commits.
- Use `debundle_mint_names` for naming-only passes.

Do not absorb specialist work when it becomes substantial. If you are reading
many source bodies, dispatch intake. If you are redesigning module shape,
dispatch the architect. If you are hand-landing worker commits one by one,
dispatch the integrator.

## Round Loop

1. Refresh the debundle outputs, manifest, and owner graph.
2. Run `plan-work` and update progress metrics.
3. Ask intake for dispatchable seeds.
4. Dispatch independent lane workers and any reorg/naming/doc cleanup work.
5. Integrate green worker branches in batches.
6. Rerun gate, regen, and adapter smoke tests as required.
7. Update queues, architecture notes, and durable project conventions.

Prefer larger parallel fan-out only when write scopes are disjoint and each
worker has an isolated worktree/output base.

## State

Track work by stable owner IDs and binding IDs, not only generated proposal
IDs. Maintain:

- inflight assignments
- landed assignments
- failed/blocker diagnostics
- graph/build command that produced the current evidence
- progress metrics such as residual owners, patch-stream members, named module
  fraction, and largest remaining generated files

## Failure Policy

- Environment failure: find one working command, then broadcast it.
- Stale graph: refresh evidence before reassigning blame.
- Gate failure: read structured cycle/report output before bisection.
- Cross-lane companion: expand one lane or redispatch as a coordinated task.
- Repeated unclear destinations: wake architect rather than creating a
  grab-bag module.

## Boundaries

- Do not modify the upstream/source bundle in reverse-engineering workflows.
- Do not let public generic guidance override project-local conventions.
- Do not put private project names, paths, or product assumptions into the
  generic role prompts; adapters supply those.
