# Plan and delegate Debundle work

Use the [task chooser](../../README.md#choose-a-task) for individual authoring
operations. This guide owns shared planning, worker, and integration procedures;
project adapters supply concrete paths, commands, conventions, and gates.

| Task                               | Read                                                                                   | Result                                                        |
| ---------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Plan an assignment without editing | [Plan an assignment](../../docs/planning.md#plan-an-assignment)                        | Source-backed proposal, decomposition, or selector worklist   |
| Ground residual proposals          | [Prepare seeds](../../docs/planning.md#prepare-seeds)                                  | Named worker packets in `seeds.json`                          |
| Apply one assigned edit            | [Execute a lane](../../docs/spec_editing.md#workflow-executing-a-delegated-assignment) | Reviewed YAML delta and completed validation evidence         |
| Combine worker results             | [Integrate worker results](../../docs/integration.md)                                  | Validated combined spec and per-branch disposition            |
| Coordinate a round                 | [Coordinate a round](#coordinate-a-round)                                              | Isolated assignments, current evidence, and routed follow-ups |

## Roles

- `debundle_plan_work`: read-only source/graph planning.
- `debundle_intake`: grounds residual proposals and names seed destinations.
- `debundle_lane_worker`: implements one scoped assignment.
- `debundle_integrator`: combines worker results in isolated integration state.
- `debundle_orchestrator`: owns adapter inputs, dispatch, and progress tracking.
- `debundle_architect`: [architecture review](../../docs/architecture_review.md)
  and worker-ready reorganization recommendations; no spec edits.
- `debundle_mint_names`: [binding naming](../../docs/naming.md) only.
- `debundle_stabilize`: [selector review](../../docs/selectors.md#review-one-selector)
  without renaming or moving modules.
- `debundle_explain`: [trace behavior](../../docs/cli.md#explain-behavior-with-a-partial-spec)
  and close only the coverage gaps needed for the question.

## Adapter Contract

The CLI guide's ["Delegating with an input
snapshot"](../../docs/cli.md#delegating-with-an-input-snapshot) and ["Resolved
invocation records"](../../docs/cli.md#resolved-invocation-records) sections define
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

## Coordinate a round

1. Refresh pipeline outputs, graphs, root/chunk/directory reports, and cycle
   evidence. Select work using [planning](../../docs/planning.md#plan-an-assignment).
2. Route residual proposals through intake. Send reviewed existing-module maps
   directly to workers; intake does not consume decomposition reports. Prefer
   coherent synthesis buckets for selector cleanup.
3. Establish each worker's isolated inputs and permitted scope using the
   [adapter contract](#adapter-contract), then dispatch the relevant role.
4. Collect completed patches and execution evidence; integrate green work using
   [the integration procedure](../../docs/integration.md).
5. Update inflight/landed/blocked assignments, architecture notes, and durable
   project conventions. Track the graph/build command and input identity behind
   metrics such as residual units, patch-stream members, named-module fraction,
   selector debt, and largest generated files.

Route substantial source discovery to intake, architecture decisions to the
architect, naming-only work to the namer, and merge trains to the integrator.
The orchestrator owns adapter details and broadcasts working commands; it should
not absorb specialist work as that work becomes substantial.

## Execution evidence

Workers report the branch/commit, starting manifest and graph/input identity,
reviewed relative YAML delta, destinations/bindings changed, skipped units and
reasons, and architecture/intake/tooling follow-ups.

For each executed command retain resolved `argv` and `cwd`, actual exit status,
`stdout`, `stderr`, and result artifacts. State which proof it covers: baseline
pipeline, proposed assignment, regeneration, selector uniqueness, runtime, or CI.
Keep those outcomes distinct and explain inapplicable checks. A command plan or
pre-dispatch invocation record does not prove execution or success; the
[invocation-record contract](../../docs/cli.md#resolved-invocation-records) defines
those fields.

## Isolation

Writers use isolated Git worktrees and build output bases; read-only roles may
inspect the main checkout. Snapshots also isolate external source/package roots.
Project conventions take precedence over generic guidance.

For each changed producer regenerated from BuildBuddy artifacts, use a fresh
clone-owned `bb-out/` download. A writer can clear its destination and still
copy stale inputs from a reused download tree, so require the adapter's
review-tree equality and normal smoke checks before accepting the output.
Preserve any prior tree outside the workspace and follow the
[downloaded-artifact recovery procedure](https://github.com/agentydragon/ducktape/blob/devel/devinfra/docs/bb_remote_internals.md#checking-a-generated-tree-after-download).

Before a Git-based remote gate, stage new spec/source/fixture files or verify
that the runner includes them. Check the logged input snapshot: omitted files
were not validated.

## Failure routing

- Environment failure: find a working adapter command and broadcast it.
- Stale graph: refresh matching evidence before diagnosing another cause.
- Gate rejection: read the structured cycle/atom reports and the source-backed
  [diagnostics](../../docs/cli.md#rejection-diagnostics). Establish initializer,
  called-binding, provider, and runtime facts before choosing an assertion via
  the [contract guide](initialization_contracts.md). A cycle alone supplies none
  of those premises. Preserve conservative constraints when the audit is not
  established, and rerun the same gate and runtime smoke after justified edits.
- Split atomic group: expand the scoped lane to include the whole group or
  redispatch coordinated work.
- Unclear destination: route to the architect; broad source discovery: intake.
- Repeated selector synthesis gaps: route to Ducktape tooling using
  [selector debt](../../docs/selectors.md#selector-debt).

If Debundle tooling blocks the task, follow the [bug reproduction
workflow](../../docs/bug_reproductions.md) before working around it.
