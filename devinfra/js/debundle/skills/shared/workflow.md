# Debundle Agent Workflow

This reference describes the reusable multi-agent workflow for AI-assisted
debundling. Project adapters supply concrete paths, commands, conventions, and
verification gates.

Use the [task chooser](README.md#choose-a-task) to select a CLI recipe before
starting a lane. Read contract guidance only when source investigation requires
an author assertion.

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

The CLI guide's ["Delegating with an input
snapshot"](docs/cli.md#delegating-with-an-input-snapshot) and ["Resolved
invocation records"](docs/cli.md#resolved-invocation-records) sections define
command semantics. This contract defines what the adapter and each role must
exchange.

Before starting a round, the project adapter should identify:

- debundle target and graph-refresh command
- modules directory, patch stream if any, emitted JS root, and source root
- dependency graph, root/chunk reports, directory reports, and cycle report
  locations
- gate, regen, uniqueness-check, and smoke-test commands
- project convention docs and taxonomy docs
- architecture notes and module reorg paths
- worktree policy, base branch, commit/push policy, and scratch paths

For each snapshot-backed lane worker, also supply:

- a unique snapshot destination and the starting `snapshot.json`, with a
  successful `snapshot verify` and the baseline pipeline result;
- the exact starting graph and input identity: graph digest, provenance for
  captured source, YAML and package inputs, engine digest verified against that
  provenance, and the spec tree/version they describe;
- the copied engine path, copied graph and spec/source roots, tree config,
  modules path, relocated graph package-root map, and the exact regeneration
  `argv`; record the working directory used for every command;
- the worker's permitted YAML subtree and exact commands for the proposed
  assignment, regeneration and runtime checks, plus CI when applicable. Keep
  baseline pipeline, proposed-assignment, post-edit, runtime and CI results
  distinct; mark an inapplicable check with its reason.

Name the command that establishes selector uniqueness, including when the full
pipeline already covers it. Capture pre-edit verification's actual result and
streams too; terminal output alone is not a durable handoff record.

The adapter supplies the exact operation-specific chunk and YAML scope. Do
not infer a module subtree from a chunk ID; follow the CLI guide for the flags
each verb accepts.

For a runnable composition of one reviewed `modules merge`, starting after
`snapshot create` and `snapshot verify`, see
[`examples/isolated_edit.py`](examples/isolated_edit.py). It prints the plan,
runs the dry-run, applies with invocation capture, and regenerates explicitly;
it is an example of that edit flow, not a generic adapter framework.

Use one writable snapshot per worker. A Git worktree does not isolate external
source or package roots. A snapshot carries the graph inputs and engine, not
the full runtime closure: adapter commands must point runtime checks at the
specific worker or integration copies being tested, and include any required
runtime inputs they copy. State unsupported relocation when a required input
cannot be copied safely. A command plan or pre-dispatch invocation record is
not proof that a command ran or passed; retain each executed command's
resolved `argv` and `cwd`, then separately save its actual exit status,
`stdout`, `stderr` and result artifacts.

Public skills must not encode private project names or fixed app taxonomies.

## Selector Portability

Selector authoring and bulk stabilization rounds follow
`devinfra/js/debundle/docs/selectors.md` § "Authoring portable selectors" (the
contract and ladder, the `selector-debt` / `synthesize-selectors` bulk loop).
Route repeated skip reasons or over-verbose generated selectors to Ducktape
tooling rather than hand-maintaining exact long bodies across many modules.

## Round Loop

1. Refresh debundle outputs, root/chunk reports, directory reports, and
   dependency graph.
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
   Hand a reviewed source-unit map directly to a lane worker for `modules refine`;
   residual groups need the separate fresh-claim workflow. Planning remains
   read-only; optional search limits retain the full candidate gate.
5. Dispatch independent lane workers and any architect/naming/doc cleanup work.
6. Integrate green worker branches in batches.
7. Rerun gate, regen, and adapter smoke tests as required.
8. Update queues, architecture notes, and durable project conventions.

Track source units by the current graph's unit IDs and binding claims by their
chunk and name, with the source/spec revisions that produced the graph. These
identities are scoped to that input program. Proposal IDs may renumber after
each integration.

Track inflight, landed, and blocked assignments; the graph/build command
that produced the evidence; and progress metrics such as residual source units,
patch-stream members, named module fraction, selector debt, and largest
remaining generated files.

## Isolation

Writing agents should use isolated git worktrees and isolated build output
bases. Read-only agents may inspect the main checkout. Integrators use a
dedicated isolated integration state at the selected receiving revision.
Do not modify the upstream bundle during reverse-engineering work. Project
conventions take precedence over generic guidance.

Before a Git-based remote gate, stage newly authored spec, source and fixture
files, or verify that the runner explicitly includes them. Check that the logged
input snapshot contains those files; a run with omitted inputs does not validate
the change.

For snapshot-backed lanes, the worker runs the copied engine against the copied
roots and package map. The initial `snapshot verify` checks the starting inputs;
after an accepted YAML edit it is expected to fail because the snapshot has
changed. The saved graph also describes the pre-edit YAML, so regenerate from
the worker copy before any later graph-backed query or edit. Do not require the
starting-state verification to pass again after editing.

The worker returns its baseline manifest, relative YAML additions,
modifications and deletions, and execution records with actual outcomes and
the proof each result covers. Before applying those changes, the integrator
checks that its receiving graph, source, spec and engine still match the
worker's baseline. If they differ, reject the result and obtain fresh evidence
from a new snapshot after rebasing or redispatching the worker. Apply changes
in an isolated integration state, then regenerate and validate the combined
spec; green worker checks do not prove the combined result.

## Failure Routing

- Environment failure: find one working command and broadcast it.
- Stale graph: refresh evidence before reassigning blame.
- Gate failure: read structured cycle/report output before bisection.
- For a side-effect cycle, start with the exact cycle edge and initializer
  evidence: identify the source unit, source location, purity rule, and called
  binding. Check whether the effect is in an eagerly evaluated initializer or
  only in a lazily called function body, then inspect the called value's
  definition and defining chunk. Unknown imported calls are conservatively
  effectful; do not infer purity from a familiar name or annotate just to make
  the cycle disappear.
- Classify the actual blocker before choosing an author assertion: effect
  purity, callback retention or synchronous reentry, runtime loading, metadata
  mutation, and global publication have different contracts. Follow
  `references/skills/shared/initialization_contracts.md` for the decision route
  and bundled `references/SPEC.md` for callback, runtime, metadata, and publication audits.
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
