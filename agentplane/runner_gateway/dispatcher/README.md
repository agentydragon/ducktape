# Session Dispatcher

The durable command queue is the Dispatcher role of the
[Runner Gateway](../README.md). The Go integration app and the Notification Service use the
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
  `POST /v1/dispatches/{command_id}:cancel`. Both transports use the same ServiceAccount TokenReview and
  per-Session owner checks; a browser reaches it through the app's authenticated command facade.
- **Delivery states** say where a command is: `QUEUED_IN_DISPATCHER`, `DELIVERING_TO_RUNNER`,
  `RUNNER_ADMITTED`, `HARNESS_CONFIRMED`, `NO_OP`, `FAILED`, `CANCELLED_IN_DISPATCHER`, `UNKNOWN` (a deleted Sandbox's).
  The `reason` describes queue or terminal state; `delivery_blocker` and `wake_assessment` separately
  expose the current environment blocker and latest typed wake observation. A denied or disabled wake
  leaves delivery queued.
- **Order**: first in, first out per Session. `InterruptTurn` is not queued: it goes to a Ready
  Sandbox at once, otherwise answers `NO_OP`. The runner rejects a stale target with
  `FAILED_PRECONDITION` before admission. It never interrupts a successor turn.
- **Opening a Session**: `OpenSession` requires the expected `sandbox_uid`, reads the current Sandbox and refuses
  a same-name replacement with `FAILED_PRECONDITION` before storing anything. It stores the request keyed by the
  caller-minted `session_id` and answers with a `SessionOpening` in the same delivery states. A retry with the same
  Sandbox name, UID, spec and `setup_script` returns the stored record; anything else under that ID is
  `ALREADY_EXISTS`. Once the Sandbox is `Ready` the runner's idempotent `OpenSession` is called before any of the Session's commands; a central sampler Session bypasses executor readiness. A
  matching runner retry returns its durable Session reservation; native preparation waits for work.
  A reservation does not assert a running harness; `ALREADY_EXISTS` means a requested spec or
  setup conflicts, which the Dispatcher records as a failed opening.
  Openings follow the rules of commands below for deleted Sandboxes, runner statuses and retention.
- **Wire rollout**: during mixed-version overlap, the old Dispatcher ignores the new protobuf field; an old caller
  reaching the required-UID Dispatcher is rejected with `INVALID_ARGUMENT` when it omits `sandbox_uid`. To roll back,
  restore the old Dispatcher before rolling back app and Action callers. Existing Session openings and running Sessions
  are unaffected.
- **Caller authority**: a normal workload token must prove the target Sandbox's exact controller-owned
  execution ServiceAccount UID. New Sessions retain that UID as their immutable logical principal, and
  retain the actual opening caller separately as creator/broker. Normal command writes, cancellation,
  and interrupt also require the recorded principal to match the live managed account UID. A queued
  delivery rechecks that UID immediately before calling the Runner. Normal receipt reads and watches
  filter by the retained UID and do not require a live Sandbox or account. The configured backend
  accounts keep their existing privileged behavior only when TokenReview also proves a Pod binding;
  an account-only token for the same name uses normal per-Session authority.
- **Open before Ready**: a configured Pod-bound backend can open a Session before the controller has
  created the execution ServiceAccount. The opening stores the expected target account with its UID
  pending. Delivery binds that UID only after confirming the account has a controller OwnerReference
  to the exact Sandbox UID. Legacy openings with no recorded owner remain unowned by normal callers and
  are never adopted based on a matching current account name.
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
- **Automatic wake**: `wakeOnSend` opts a Sandbox into wake attempts; it is not a permission grant.
  Before each automatic attempt, the shared lifecycle service checks the live target UID, current
  execution ServiceAccount UID and `resumePolicy`, or the separately configured trusted wake broker
  grant against the row's retained effective sender. Dispatcher actor identity and admission-time
  eligibility do not grant wake authority. A `SuspendedBy=Requested` stop stays sticky for ordinary
  sends and automatic wakes. `InterruptTurn` never wakes.

  Each queued Dispatch and Session opening stores a typed `delivery_blocker` and `wake_assessment`.
  The receipt remains `QUEUED_IN_DISPATCHER` through disabled, denied, rate-limited, failed and
  uncertain wake outcomes; those observations never become command failures. `observed_at`, target
  generation, optional retry time and structured status details describe the latest decision. A
  rate-limited result is retried at its shared-budget `retry_at`. An uncertain patch response is not
  repeated for the same observed generation; the worker reassesses after that generation changes.
  The lifecycle service and PostgreSQL budget adapter share a limit of 10 distinct guarded wake
  generations per effective ServiceAccount UID in a rolling 60-second window, scoped by target
  Sandbox UID and generation. Retries of a generation reuse its reservation. After a successful wake
  request, delivery waits for the Sandbox to become `Ready`; if it remains unready for ten minutes,
  the existing queue deadline fails the work with the current Ready condition details. A central
  sampler Session bypasses execution-Sandbox readiness and wake so its input remains deliverable
  while the execution environment is suspended.

Delivery wakes on a stored command (PostgreSQL `NOTIFY`), a Sandbox change (the `ListWatch` on
Sandboxes) or a retry deadline. Every ten seconds each replica also renews its owned Leases and retries
the Sandboxes with pending work that it does not own.

## Deviations and gaps

- `HARNESS_CONFIRMED` has no writer, so inputs stay `RUNNER_ADMITTED`.
- Runner Pods still reach external services through the existing egress proxy and do not have a
  Dispatcher route or Dispatcher bearer projected into their sidecar. The next transport integration
  should add the Dispatcher audience to the sidecar-only projection in
  [`sandbox_pod.py`](../../../cluster/cdk8s/agentplane/sandbox_pod.py), add the exact Dispatcher
  route and credential mapping in [`egress.py`](../../../cluster/cdk8s/agentplane/egress.py), and
  connect a concrete MCP/client caller to the HTTPS proxy placeholder. Do not expose the real bearer
  to the agent container or add direct Runner egress. The MCP/client caller and History provenance
  propagation are not part of this Dispatcher authorization change.

The detailed ownership, compatibility and transport contract is in [SPEC.md](SPEC.md).

## Running

The serving binary and image live under `//agentplane/runner_gateway` and
`//agentplane/runner_gateway:image`. It reads the audience and caller settings described in
<../README.md>, a projected Gateway token for calls to other holder replicas, and
`AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL`. The serving binary applies the embedded Goose queue migrations before opening the Gateway
listener; the same binary accepts `--migrate-only` for explicit fixture or maintenance use. The
startup probe waits for initialization before `grpc.health.v1` readiness on the shared listener.
