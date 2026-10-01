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
  `debundle` graph/source queries (`describe`, `show-source`, `atoms`,
  `coverage`, `modules propose`, `scc`, `cluster`, `graph-summary`);
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
2. Run `debundle modules propose` and `graph-summary` for aggregate
   graph metrics. Use `graph-summary --include-proposals` only when
   proposal and diagnostic counts are needed.
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
- For a side-effect cycle, start with the exact cycle edge and initializer
  evidence: identify the owner, source location, purity rule, and called
  binding. Check whether the effect is in an eagerly evaluated initializer or
  only in a lazily called function body, then inspect the called value's
  definition and defining chunk. Unknown imported calls are conservatively
  effectful; do not infer purity from a familiar name or annotate just to make
  the cycle disappear.
- Use a purity annotation only when source or API behavior establishes the
  required contract. Choose the narrowest supported form: `purity: pure` for a
  local bound function, `purity: pure_new` only for construction, or
  `chunk_export_purity.<defining chunk>.pure_exports` for a named cross-chunk
  export. Use `pure_members` for a specific static member call on an imported
  namespace, and `fluent_exports` only when the whole transitive fluent
  surface meets its broader contract. These are author-trusted assertions,
  not body verification; argument expressions are still analyzed, and an
  export's initializer is not declared pure by a call-purity annotation. See
  `references/purity_annotations.md` for the cycle-recovery decision steps and
  examples; the full contracts are in `devinfra/js/debundle/docs/design.md`
  § A9 and `devinfra/js/debundle/docs/purity_soundness.md`.
- After annotating, rerun the same gate and confirm the reported side-effect
  edge is gone and the intended assignment becomes realizable. If the call
  can have observable effects during module initialization, change the
  assignment boundary instead of asserting purity.
- Split atomic unit: expand one lane to cover the whole unit or redispatch as
  coordinated work.
- Unclear destination: route to architect instead of creating a grab-bag.
- Broad source understanding needed: route to intake instead of overloading a
  lane worker.
