# MCP tasks for canonical Actions

## Scope

Implement **MCP tasks first**, using the protocol FastMCP 4.0.3 supports:
`io.modelcontextprotocol/tasks` (SEP-2663). `start_action_task` is the task-only
submission tool, with just the flat canonical Action input fields. It requires
negotiated task support and returns a flat task-creation result. `tasks/get`
returns status and, when complete, the final result inline. `tasks/cancel`
requests cancellation; `tasks/update` is unsupported for Action-backed tasks
because Actions cannot pause for client input. The existing `request_action`
receipt/wait workflow and HTTP Action API stay unchanged.

**Out of scope:** executor-reported progress, numeric percentages, progress
notifications, partial output, and an update stream. Decide whether to
build any of these separately, later. Task status comes only from the
existing durable Action states, not from heartbeats or invented progress.
An MCP task is useful without a progress feature.

## FastMCP seam

Implement an Agentplane-specific `ServerExtension` registered by
`create_server()` in `mcp_frontend.py`. Use the FastMCP extension's
`settings`, `methods` (`MethodBinding`) and `intercept_tool_call` hooks.
The extension advertises `io.modelcontextprotocol/tasks` only when installed.
Keep `FastMCP(tasks=False)` as the global default and opt **only**
`start_action_task` into task support. The old `request_action`, dynamic direct
tools, other Action tools, and ordinary calls are not intercepted. Do not use the
optional `fastmcp-tasks` Docket execution queue: the Action Service already
has its own durable queue, dispatcher and executor leases. This does not
require an MCP SDK fork or implementing the old `tasks/result` protocol.

The interceptor should:

1. Intercept only `start_action_task` when the protocol version and per-request
   tasks extension are negotiated. All other tools call `call_next()` and
   preserve their old behavior. Without task negotiation, the task-only tool
   itself refuses the call without submitting.
2. Authenticate through the existing `CallerTokenVerifier`/`CallerToken`
   context. Validate the tool's flat Action fields with `ActionRequestInput`
   before submission. `wait`, `respond_with`, `include_fields`, and a nested
   `request` are not in its schema.
3. Submit through `ActionService.submit` exactly once with the caller's
   supplied idempotency key, using the normal policy/approval path.
   Return a SEP-2663 `CreateTaskResult` with a task ID derived from the
   canonical Action request UUID. Do **not** invoke `call_next()` after
   submission, enqueue a worker, or re-execute an Action during polling.

Register handlers for `tasks/get`, `tasks/cancel`, and `tasks/update`,
using the extension's protocol-version and per-request capability gates.
Task methods authenticate on _every_ request and check ownership through
the same caller-scoped `ActionService.get` path as existing MCP reads. A
foreign task ID must look like not-found. An update request returns a clear
unsupported-input error; it must not invent an `input_required` state.
Conformance tests must exercise real JSON-RPC over the `/mcp` mount,
including task creation, follow-up reads, legacy `request_action` calls,
and refusal of non-task calls to `start_action_task` without side effects.

## Canonical Action is the execution engine

`ActionService.submit` evaluates admission and persists one
`ActionRequestRow`. The service dispatch loop claims an `ExecutionRow`;
`_execute_claim` invokes the group's `Executor.execute(request, lease)` and
persists an `ExecutionResult`. An MCP executor calls an upstream MCP tool;
a sandbox executor runs a sandbox Action. An MCP task neither schedules
another execution nor owns the executor lease.

Make the Action request UUID the MCP task ID (or a stable encoding). A lost
creation response is recovered through the existing owner-scoped lookup
by the original idempotency key; a repeated key remains refused. A client
must not be instructed to submit a replacement key. The existing `/mcp`
transport is stateless Streamable HTTP, so task reads must work after a
transport disconnect, restart, or request landing on a different replica.
No task-worker state, task marker, separate task row, or caller bearer is
stored: any existing Action owned by the caller can be read by ID as a task.
Task retention follows the canonical Action and its append-only event history;
never expire an active Action or encourage another execution.

| Action state                        | Task state  | Meaning                                             |
| ----------------------------------- | ----------- | --------------------------------------------------- |
| `decision_pending`                  | `working`   | Awaiting approval; nothing ran.                     |
| `allowed`, `dispatching`, `running` | `working`   | Approved / claimed / executing.                     |
| `succeeded`                         | `completed` | Inline the final tool result, preserving `isError`. |
| `denied`, `failed`                  | `failed`    | Give a distinct safe diagnostic.                    |
| `cancelled`                         | `cancelled` | Withdrawn before dispatch.                          |
| `execution_unknown`                 | `failed`    | May have run; never retry automatically.            |

For `tasks/get`, adapt the existing `tool_result` conversion to preserve
MCP content blocks (including images), structured content and `isError`,
and sandbox results. The upstream tool can return `isError=true` even
though the Action execution state is `succeeded`. FastMCP's SEP-2663
backend reports a completed task with that original tool error result
inlined; preserve that behavior.
A status message may say "waiting for operator approval" or "running",
but must not claim execution progress beyond those states.

MCP terminal task states cannot change. An `execution_unknown` Action may
later be reconciled by an authenticated late completion or authority
lookup. Read the earliest terminal event (ordered by sequence) from the
append-only Action event history to determine the task's immutable state and
timestamp. This works for Actions created before task support as well. A
successful Action cannot transition again, so its execution result is read
from that same durable row; for a failed task the diagnostic derives from the
first terminal event, not from a later reconciliation. The Action receipt may
later reflect the reconciled truth; the task cannot change a published terminal
answer. No new database columns or migration are needed. Test this race.

`tasks/cancel` delegates to `ActionService.cancel`, whose atomic store
operation can only withdraw before execution is claimed. Only
`cancelled`/`already_cancelled` make a task cancelled. `too_late` must
return an error rather than pretending to stop an executor;
`already_finished` leaves the terminal outcome intact. Disconnecting a
client does not cancel its Action.

## Tests and delivery

1. Implement the FastMCP extension and model its SEP-2663 wire shapes.
   Check capability negotiation, `request_action` opt-in only, ordinary
   calls, malformed arguments before submission, and `tasks/update`
   unsupported behavior via the actual `/mcp` endpoint.
2. Exercise pending approval, allowed, running, final success, backend
   error, upstream tool `isError`, denial, unknown outcome and late
   reconciliation. Ensure task reads and inlined results are authorized,
   immutable after terminal, and work across reconnects and replicas.
3. Exercise lost create responses and idempotency recovery, concurrent
   creation/cancellation races, and `too_late` without duplicate work.
   Run the relevant Bazel and HTTP acceptance tests before advertising
   the extension.

Deferred, separately scoped work:

- Cooperative cancellation inside executors that support it after dispatch.
- Proxy an upstream MCP task through a single canonical Action; the current
  MCP adapter uses a conventional `call_tool_mcp`.
- Task-augment dynamically exposed direct tools after defining their
  auto-approval-only refusal and bounded-wait semantics.
- **If later desired**, design Action progress/partial output independently
  of MCP tasks. It would need its own authorized storage and reporting
  contract; nothing here promises it.

## Test preflight

Pre-commit passed for plan edits. A baseline remote
`bbr test //agentplane/action_service:test_mcp_frontend` could not run:
without a BuildBuddy API key the client refused; using the sandbox egress
placeholder in `BUILDBUDDY_API_KEY` caused remote Bazel to reject the
literal. The sandbox proxy does not provision a key inside the remote
runner. Do not repeat that credential pattern; use PR CI or an approved
runner configuration. This preflight did not validate runtime tests.
