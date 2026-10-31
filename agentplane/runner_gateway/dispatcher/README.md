# Session Dispatcher

The durable command queue is the Dispatcher role of the
[Runner Gateway](../README.md). The Python App and the Notification Service use the
Session Dispatcher gRPC contract; the stable Dispatcher Service maps its port `8080`
to the shared listener on `8080` and its port `8081` to the HTTP/JSON facade. The worker delivers through the Runner proxy once
the exact Sandbox is ready, with per-Sandbox Lease and PostgreSQL generation fencing.

The manual queue, replica and wake probes are documented in the
[system tests](../../system_tests/README.md#dispatcher-queue-and-replicas).

## Contract

- **One RPC per command**: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn`,
  `OpenSession`, `GetSessionOpening`, plus `GetDispatch`, `ListDispatches`, `WatchDispatches` and `CancelDispatch`
  ([proto](session_dispatcher.proto)). Callers name the Sandbox and the Session; `command_id` is
  the idempotency key across every command RPC.
- **Watching**: `WatchDispatches` sends the Dispatches in scope, then one `CaughtUp` frame, then a Dispatch
  again whenever it changes. `DispatchFilter.command_ids` (at most 1000) narrows either scope to those
  commands, so a caller holding a few of them does not replay a whole Session.
- **HTTP/JSON form**: every RPC also answers over HTTP on the Gateway's optional `http_listen_address` setting,
  [generated from the proto](../../docs/grpc_services.md#httpjson-facades): reads under `/v1/dispatches`
  and `/v1/sessions/{session_id}/opening`, commands as `POST /v1/sessions/{session_id}:<verb>` and
  `POST /v1/dispatches/{command_id}:cancel`. The Dispatcher admits only the configured ServiceAccounts
  on either form; a browser reaches it through the app, whose Thread checks stay in front of commands.
- **Delivery states** say where a command is: `QUEUED_IN_DISPATCHER`, `DELIVERING_TO_RUNNER`,
  `RUNNER_ADMITTED`, `HARNESS_CONFIRMED`, `NO_OP`, `FAILED`, `CANCELLED_IN_DISPATCHER`, `UNKNOWN` (a deleted Sandbox's). A
  `Dispatch`'s `reason` says what a queued command waits for or where a terminal one ended.
- **Order**: first in, first out per Session. `InterruptTurn` is not queued: it goes to a Ready
  Sandbox at once, otherwise answers `NO_OP`. The runner rejects a stale target with
  `FAILED_PRECONDITION` before admission. It never interrupts a successor turn.
- **Opening a Session**: `OpenSession` stores the request keyed by the caller-minted `session_id` and answers
  with a `SessionOpening` in the same delivery states. A retry with the same Sandbox, spec and
  `setup_script` returns the stored record; anything else under that ID is `ALREADY_EXISTS`. Once the Sandbox
  is `Ready` the runner's idempotent `OpenSession` is called before any of the Session's commands. A
  matching runner retry returns its durable Session reservation; native preparation waits for work.
  A reservation does not assert a running harness; `ALREADY_EXISTS` means a requested spec or
  setup conflicts, which the Dispatcher records as a failed opening.
  Openings follow the rules of commands below for deleted Sandboxes, runner statuses and retention.
- **Queue**: no TTL; at most `max_queued_per_session` queued commands per Session, then
  `RESOURCE_EXHAUSTED`. A command for a Sandbox that does not exist is refused `NOT_FOUND` and stores nothing.
- **Sandbox deleted** (seen on the Sandbox watch): queued commands end `FAILED` (`sandbox_deleted`), delivering
  ones and admitted inputs `UNKNOWN`. The Dispatcher publishes their exact protobuf snapshots through
  transactional PostgreSQL notifications in bounded chunks, in the same transaction that deletes the rows,
  so active `WatchDispatches` streams on any connected replica receive them. A listener disconnect, reconnect
  gap or incomplete event fails active streams with `UNAVAILABLE`; terminal events have no durable replay,
  so a later read or watch is `NOT_FOUND`/empty.
- **Retention**: a Sandbox's Dispatches are kept as long as it exists. Other policies may come later.
- **Replica ownership**: a per-Sandbox Lease assigns delivery scheduling ownership while the Sandbox
  has a `QUEUED_IN_DISPATCHER` or `DELIVERING_TO_RUNNER` Dispatch or Session opening. Its PostgreSQL
  generation fences stale claims, settlements and recovery. A Sandbox without such work is no longer
  renewed and its Lease lapses; the next work reacquires it. The Runner Gateway's separate connection
  Lease still owns the runner stream; Dispatcher scheduling does not affect it. A new owner requeues
  only that Sandbox's in-flight work, preserving at-least-once redelivery with the same command IDs.
- **Runner statuses**: `ABORTED` (quiescing) returns the command to the queue until the Sandbox has a new
  incarnation; `UNAVAILABLE` and deadline retry with backoff;
  anything else is `FAILED` with the runner's status. The runner deduplicates redelivery on `command_id`.
- **Send wake**: only work from authenticated identities listed in `wake_caller_accounts` may request a
  wake, and the Sandbox must also have `spec.wakeOnSend: true`. Both fields default off. The Dispatcher
  stores the authenticated caller on eligible commands and openings, then reserves a wake budget in
  PostgreSQL immediately before the shared guarded resume helper patches a suspended Sandbox. The
  initial budget is 10 distinct wake generations per authenticated caller in a rolling 60-second
  window, shared across replicas and restarts. The charge key is caller, Sandbox UID and target
  `wakeGeneration`: messages for the same Sandbox resume share one charge, and retries of a target
  generation reuse its charge while it remains in the window. A guarded patch reservation is charged
  even if its response fails; after its minute expires, a later real patch attempt may consume another
  slot. A denied attempt stays queued with `wake_rate_limited` and retries when the oldest charge
  expires. A stored wake-eligible row without an authenticated caller fails closed as
  `wake_caller_unknown`. The Dispatcher delivers only after a different incarnation becomes
  `Ready`; `InterruptTurn` never wakes. A queued command or session opening
  that remains unready for ten minutes after its budgeted wake request becomes `FAILED`, with the
  Sandbox's current `Ready` condition reason and message recorded for the sender. The queue row is
  retained, and quota wait time does not count toward the deadline. The budget does not expand the
  caller allowlist. The initial limit is ten wake attempts per minute per authenticated caller; tuning
  remains a follow-up.

Delivery wakes on a stored command (PostgreSQL `NOTIFY`), a Sandbox change (the `ListWatch` on
Sandboxes) or a retry deadline. Every ten seconds each replica also renews its owned Leases and retries
the Sandboxes with pending work that it does not own.

## Deviations and gaps

- `HARNESS_CONFIRMED` has no writer, so inputs stay `RUNNER_ADMITTED`.
- Any account in `caller_accounts` may send to any Sandbox.

## Running

The serving binary and image live under `//agentplane/runner_gateway` and
`//agentplane/runner_gateway:image`. It reads the audience and caller settings described in
<../README.md>, a projected Gateway token for calls to other holder replicas, and
`AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL`. The serving binary applies the embedded Goose queue migrations before opening the Gateway
listener; the same binary accepts `--migrate-only` for explicit fixture or maintenance use. The
startup probe waits for initialization before `grpc.health.v1` readiness on the shared listener.
