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
- Planner/namer skills: `debundle_plan_work` handles read-only
  `debundle` graph/source queries (`describe`, `inspect-source --id`, `atoms`,
  `coverage`, `modules propose`, `modules decompose`, `scc`, `cluster`, `graph-summary`);
  `debundle_mint_names` handles naming-only edits.

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

## Selector Portability

Selector authoring and bulk stabilization rounds follow
`devinfra/js/debundle/docs/selectors.md` § "Authoring portable selectors" (the
contract and ladder, the `selector-debt` / `synthesize-selectors` bulk loop).
Route repeated skip reasons or over-verbose generated selectors to Ducktape
tooling rather than hand-maintaining exact long bodies across many modules.

## Round Loop

1. Refresh debundle outputs, root/chunk reports, directory reports, and
   owner graph.
2. Use `graph-summary` for orientation. Choose `modules propose` for residual
   extraction or `modules decompose --atomic` for existing-module layout work.
   Use `graph-summary --include-proposals` when proposal counts are needed.
3. For selector cleanup, run `selector-debt` with module grouping. Route
   repeated synthesis gaps to Ducktape tooling rather than asking workers to
   maintain exact generated bodies.
4. Route proposal rows to intake for dispatchable seeds. `landable_today: true`
   is graph readiness; workers still prove selectors and run the edit gate.
   For decomposition, review the complete candidate, source-backed selector
   anchors, semantic destinations, and any coarsening/rejoining results.
   Hand a reviewed owner map directly to a lane worker for `modules refine`;
   residual groups need the separate fresh-claim workflow. Planning remains
   read-only; optional search limits retain the full candidate gate.
5. Dispatch independent lane workers and any architect/naming/doc cleanup work.
6. Integrate green worker branches in batches.
7. Rerun gate, regen, and adapter smoke tests as required.
8. Update queues, architecture notes, and durable project conventions.

Track work by stable owner IDs and binding IDs, not only generated proposal
IDs. Proposal IDs may renumber after each integration.

Track inflight, landed, and blocked assignments; the graph/build command
that produced the evidence; and progress metrics such as residual owners,
patch-stream members, named module fraction, selector debt, and largest
remaining generated files.

## Isolation

Writing agents should use isolated git worktrees and isolated build output
bases. Read-only agents may inspect the main checkout. Integrators are the
exception: they operate deliberately on the shared integration branch.
Do not modify the upstream bundle during reverse-engineering work. Project
conventions take precedence over generic guidance.

## Failure Routing

- Environment failure: find one working command and broadcast it.
- Stale graph: refresh evidence before reassigning blame.
- Gate failure: read structured cycle/report output before bisection.
- For a side-effect cycle, start with the exact cycle edge and initializer
  evidence: identify the owner, source location, purity rule, and called
  binding. Check whether the effect is in an eagerly evaluated initializer or
  only in a lazily called function body, then inspect the called value's
  definition and defining chunk. Unknown imported calls are conservatively
  effectful; do not infer purity from a familiar name or annotate just to make
  the cycle disappear.
- Classify the actual blocker before choosing an author assertion: effect
  purity, callback retention or synchronous reentry, runtime loading, metadata
  mutation, and global publication have different contracts. Follow
  `references/initialization_contracts.md` for the decision route and bundled
  `references/SPEC.md` for callback, runtime, metadata, and publication audits.
  A cycle alone establishes none of these premises.
- After a justified annotation, rerun source validation, the same extraction
  gate, and the adapter's runtime smoke. Inspect the remaining constraints;
  an assertion may narrow callback availability while retaining eager reads,
  mutation, and ordering. When the required audit cannot be established, keep
  the conservative constraints and change the module boundary.
- Split atomic group: expand one lane to cover the whole group or redispatch as
  coordinated work.
- Unclear destination: route to architect instead of creating a grab-bag.
- Broad source understanding needed: route to intake instead of overloading a
  lane worker.
