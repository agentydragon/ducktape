# Session Dispatcher

The durable queue between senders and a Sandbox's runner. A sender stores a command and gets a
`Dispatch` back at once; the Dispatcher delivers it through the Runner Gateway's runner proxy once the
Sandbox is `Ready`, and the sender reads where it is. Control design:
[ADR](../docs/adr_sandbox_session_control.md). Not deployed yet; what remains is in
[`DISPATCHER_CORE`](../plans/task_dag.md).

## Contract

- **One RPC per command**: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn`,
  `StopRunnerSession`, plus `GetDispatch`, `ListDispatches`, `WatchDispatches` and `CancelDispatch`
  ([proto](../proto/session_dispatcher.proto)). Callers name the Sandbox and the Session; `command_id` is
  the idempotency key across every command RPC.
- **Delivery states** say where a command is: `QUEUED_IN_DISPATCHER`, `DELIVERING_TO_RUNNER`,
  `RUNNER_ADMITTED`, `HARNESS_CONFIRMED`, `NO_OP`, `FAILED`, `CANCELLED_IN_DISPATCHER`, `UNKNOWN` (nothing writes it yet). A
  `Dispatch`'s `reason` says what a queued command waits for or where a terminal one ended.
- **Order**: first in, first out per Session. `StopRunnerSession` goes first and cancels the inputs queued
  before it (reason `stopped`). `InterruptTurn` is not stored: it goes to a `Ready` Sandbox at once, and
  otherwise answers `NO_OP`.
- **Queue**: no TTL; at most `max_queued_per_session` queued commands per Session, then
  `RESOURCE_EXHAUSTED`. A command queued for a Sandbox that does not exist is stored `FAILED` with reason
  `sandbox_deleted`.
- **Retention**: a Sandbox's Dispatches are kept as long as the Sandbox exists and deleted when its CR is
  deleted (seen on the Sandbox watch); a read after that is `NOT_FOUND`. Other policies may come later.
- **Runner statuses**: `ABORTED` (quiescing) returns the command to the queue until the Sandbox has a new
  incarnation; `UNAVAILABLE` and deadline retry with backoff; `FAILED_PRECONDITION` on a stop is `NO_OP`;
  anything else is `FAILED` with the runner's status. The runner deduplicates redelivery on `command_id`.

Nothing polls: delivery wakes on a stored command (PostgreSQL `NOTIFY`), a Sandbox change (the
`ListWatch` on Sandboxes) or a retry deadline.

## Deviations and gaps

- One active replica: the delivery loop assumes it is the only one.
- `HARNESS_CONFIRMED` has no writer, so inputs stay `RUNNER_ADMITTED`.
- Any account in `caller_accounts` may send to any Sandbox.

## Running

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`. Settings are the `Settings` class
(`AGENTPLANE_SESSION_DISPATCHER_CONFIG_FILE`): its own PostgreSQL database, the namespace of the Sandboxes,
the runner control target (`{sandbox}-control.{namespace}.svc:8080`) with a projected token the gateway admits,
and the callers' accounts and audience. Run `:migrate` before starting; `grpc.health.v1` is served.
