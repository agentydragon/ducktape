# Debundle Agent Workflow

This reference describes the reusable multi-agent workflow for AI-assisted
debundling. Project adapters supply concrete paths, commands, conventions, and
verification gates.

## Roles

- Orchestrator: refreshes evidence, dispatches roles, tracks state, routes
  failures, and owns adapter-specific commands.
- Intake: converts planner output into named seed clusters for workers.
- Lane worker: applies one scoped spec edit or reorg task in an isolated
  worktree.
- Architect: audits named modules and emitted tree shape, treats current names
  and splits as fallible evidence, infers and maintains path/taxonomy
  conventions from source behavior, and writes current-state architecture notes
  and reorg recommendations.
- Integrator: lands worker branches through a validated merge train.
- Planner/namer skills: `debundle_plan_work` handles `debundle peel`
  graph/source queries; `debundle_mint_names` handles naming-only edits.

## Adapter Contract

Before starting a round, the project adapter should identify:

- debundle target and graph-refresh command
- modules directory, patch stream if any, emitted JS root, and source root
- owner graph, root/chunk reports, directory reports, and cycle report
  locations
- gate, regen, uniqueness-check, and smoke-test commands
- project convention docs and taxonomy docs
- architecture notes and module reorg paths
- worktree policy, base branch, commit/push policy, and scratch paths

Public skills must not encode private project names or fixed app taxonomies.

## Round Loop

1. Refresh debundle outputs, root/chunk reports, directory reports, and
   owner graph.
2. Run `plan-work` and capture progress metrics.
3. Ask intake for dispatchable seeds.
4. Dispatch independent lane workers and any architect/naming/doc cleanup work.
5. Integrate green worker branches in batches.
6. Rerun gate, regen, and adapter smoke tests as required.
7. Update queues, architecture notes, and durable project conventions.

Track work by stable owner IDs and binding IDs, not only generated proposal
IDs. Proposal IDs may renumber after each integration.

## Isolation

Writing agents should use isolated git worktrees and isolated build output
bases. Read-only agents may inspect the main checkout. Integrators are the
exception: they operate deliberately on the shared integration branch.

## Failure Routing

- Environment failure: find one working command and broadcast it.
- Stale graph: refresh evidence before reassigning blame.
- Gate failure: read structured cycle/report output before bisection.
- Cross-lane companion: expand one lane or redispatch as coordinated work.
- Unclear destination: route to architect instead of creating a grab-bag.
- Broad source understanding needed: route to intake instead of overloading a
  lane worker.
