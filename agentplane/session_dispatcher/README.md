# Session Dispatcher

The durable queue between senders and a Sandbox's runner. A sender stores a command and gets a
`Dispatch` back at once; the Dispatcher delivers it through the Runner Gateway's runner proxy once the
Sandbox is `Ready`, and the sender reads where it is. The manifests configure one active replica in
staging and testing until `DISPATCHER_REPLICA_ACTIVATION` passes. The delivery loop shards work by
per-Sandbox Lease and fences claims and settlements with a PostgreSQL ownership generation. The app
and Notification Service still send through the Runner Gateway pending their cutovers. Control design:
[ADR](../docs/adr_sandbox_session_control.md). Deployment and production-startup acceptance evidence is in
[the acceptance report](debug/deployment_acceptance.md); replica-count activation remains gated in the
[task DAG](../plans/task_dag.md).

## Contract

- **One RPC per command**: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn`,
  `StopRunnerSession`, `OpenSession`, `GetSessionOpening`, plus `GetDispatch`, `ListDispatches`, `WatchDispatches` and `CancelDispatch`
  ([proto](../proto/session_dispatcher.proto)). Callers name the Sandbox and the Session; `command_id` is
  the idempotency key across every command RPC.
- **Delivery states** say where a command is: `QUEUED_IN_DISPATCHER`, `DELIVERING_TO_RUNNER`,
  `RUNNER_ADMITTED`, `HARNESS_CONFIRMED`, `NO_OP`, `FAILED`, `CANCELLED_IN_DISPATCHER`, `UNKNOWN` (a deleted Sandbox's). A
  `Dispatch`'s `reason` says what a queued command waits for or where a terminal one ended.
- **Order**: first in, first out per Session. `StopRunnerSession` goes first and cancels the inputs queued
  before it (reason `stopped`). `InterruptTurn` is not stored: it goes to a `Ready` Sandbox at once, and
  otherwise answers `NO_OP`.
- **Opening a Session**: `OpenSession` stores the request keyed by the caller-minted `session_id` and answers
  with a `SessionOpening` in the same delivery states. A retry with the same Sandbox, spec and
  `setup_script` returns the stored record; anything else under that ID is `ALREADY_EXISTS`. Once the Sandbox
  is `Ready` the runner's idempotent `OpenSession` is called before any of the Session's commands; the
  runner's `ALREADY_EXISTS` counts as opened when `ListSessions` holds the Session and is `FAILED` otherwise.
  Openings follow the rules of commands below for deleted Sandboxes, runner statuses and retention.
- **Queue**: no TTL; at most `max_queued_per_session` queued commands per Session, then
  `RESOURCE_EXHAUSTED`. A command for a Sandbox that does not exist is refused `NOT_FOUND` and stores nothing.
- **Sandbox deleted** (seen on the Sandbox watch): queued commands end `FAILED` (`sandbox_deleted`), delivering
  ones and admitted inputs `UNKNOWN`, which the open `WatchDispatches` streams are handed; the Sandbox's
  Dispatches are deleted in the same step, so a later read or watch is `NOT_FOUND`/empty. A stream on another
  replica is not handed them.
- **Retention**: a Sandbox's Dispatches are kept as long as it exists. Other policies may come later.
- **Replica ownership**: a per-Sandbox Lease assigns delivery scheduling ownership. Its PostgreSQL
  generation fences stale claims, settlements and recovery. The Runner Gateway's separate connection
  Lease still owns the runner stream; Dispatcher scheduling does not affect it. A new owner requeues
  only that Sandbox's in-flight work, preserving at-least-once redelivery with the same command IDs.
- **Runner statuses**: `ABORTED` (quiescing) returns the command to the queue until the Sandbox has a new
  incarnation; `UNAVAILABLE` and deadline retry with backoff; `FAILED_PRECONDITION` on a stop is `NO_OP`;
  anything else is `FAILED` with the runner's status. The runner deduplicates redelivery on `command_id`.
- **Send wake**: only work from authenticated identities listed in `wake_caller_accounts` may request a
  wake, and the Sandbox must also have `spec.wakeOnSend: true`. Both fields default off. The Dispatcher
  stores the old incarnation before using the shared guarded resume helper, retries that helper after
  transient failures and restarts, and delivers only after a different incarnation becomes `Ready`.
  `InterruptTurn` and `StopRunnerSession` never wake. Per-sender rate and quota policy remains a separate
  follow-up and must be decided before this setting is enabled.

Delivery wakes on a stored command (PostgreSQL `NOTIFY`), a Sandbox change (the `ListWatch` on
Sandboxes) or a retry deadline. Each replica also renews its owned Leases every ten seconds.

## Deviations and gaps

- Deleted-Sandbox terminal events are handed only to streams on the replica that processed the deletion.
  Cross-replica terminal broadcasts and increasing the active replica count are separate rollout gates in
  [`task_dag.md`](../plans/task_dag.md).
- `HARNESS_CONFIRMED` has no writer, so inputs stay `RUNNER_ADMITTED`.
- Any account in `caller_accounts` may send to any Sandbox.

## Running

Bazel targets: `:server`, `:migrate`, `:image`, `:migration_image`. Settings are the `Settings` class
(`AGENTPLANE_SESSION_DISPATCHER_CONFIG_FILE`): its own PostgreSQL database, the namespace of the Sandboxes,
the runner control target (`{sandbox}-control.{namespace}.svc:8080`) with a projected token the gateway admits,
and the callers' accounts and audience. `wake_caller_accounts` is optional and empty by default; configure it
only after the accepting Dispatcher and Sandbox Controller images are pinned and Dispatcher Sandbox `patch`
permission is reconciled. Run `:migrate` before starting; `grpc.health.v1` is served.
