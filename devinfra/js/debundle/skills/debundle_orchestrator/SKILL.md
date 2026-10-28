---
name: debundle_orchestrator
description: Coordinate a generic AI-driven debundling loop across intake, lane worker, architect, integrator, planning, and naming skills. Use for multi-agent debundle rounds, work routing, graph refreshes, gate/regen command broadcast, progress tracking, and adapter-specific workflow control.
---

# Debundle Orchestrator

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

Use this role to keep an AI-driven debundling loop moving. The orchestrator
routes work between specialist roles and owns project-adapter details.

Use the [task chooser](references/README.md#choose-a-task) to route each lane to
its command recipe; read the linked reference for that operation.

Read other bundled references as needed:

- `references/workflow.md` for the adapter contract, round loop, state, and failure routing
- `references/docs/bazel_integration.md` for the `debundle_pipeline` rule and profiling targets
- `references/module_shape.md` for when to route to architect or lane workers
- `references/docs/selectors.md` for the bar selector-stabilization work must meet

## Role Routing

- Use `debundle_plan_work` to refresh planner evidence.
- Send `modules propose` output to `debundle_intake` for seeds.
- For existing-module reorganization, ask the planner for a source-backed
  `modules decompose` candidate, including selector anchors and optional
  coarsening/rejoining as needed. Route the reviewed semantic source-unit assignment
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

## Snapshot Handoffs

For each lane, follow the snapshot-backed adapter contract in
`references/workflow.md`. Run and record the baseline pipeline, create a
separate worker snapshot, and verify it before dispatch. Give the worker its
baseline manifest, copied engine and relocated graph/spec/source/package
roots, exact regeneration `argv` and command working directory, permitted YAML
scope, and distinct proposed-assignment, post-edit, runtime and CI checks.
State unsupported relocation explicitly.

Collect the worker's baseline manifest, relative YAML patch and execution
records, including actual outcomes and the proof each check covers. The CLI
guide's "Resolved invocation records" section explains the record fields:
`--print-invocation` does not dispatch the command, and `--invocation-out`
records it before execution, so neither alone proves a check passed.

Do not absorb specialist work when it becomes substantial. If you are reading
many source bodies, dispatch intake. If you are redesigning module shape,
dispatch the architect. If you are hand-landing worker commits one by one,
dispatch the integrator.
