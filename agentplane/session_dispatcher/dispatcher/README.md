# Session Dispatcher

The durable command queue is the Dispatcher role of the
[Sessions binary](../README.md). The Go integration app and the Notification Service use the
Session Dispatcher gRPC contract; the stable `sessions` Service maps port `8080` to the binary's
shared TLS listener. The credential-holding Sandbox relay reaches that same listener through
central-proxy CONNECT and presents its Runner SVID. Dispatcher methods retain their caller rules,
exact service grants and typed delegation policy. EnvironmentService has its own listener. The worker
delivers through the Environments Runtime proxy once the exact Sandbox is ready, with a
per-Session PostgreSQL delivery lease.

The manual queue, replica and wake probes are documented in the
[system tests](../../system_tests/README.md#dispatcher-queue-and-replicas).

## Contract

- **One RPC per command**: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, and `InterruptTurn`.
  `CreateSession`, `GetSession`, and `ListSessions` create and read logical Session records; the
  other Session RPCs operate on them. `GetDispatch`, `ListDispatches`, `WatchDispatches`, and
  `CancelDispatch` read or cancel command receipts
  ([proto](api/session/session_dispatcher.proto)). Commands name the Session; Dispatcher resolves its placement.
  A broker with an incarnation-scoped grant supplies `expected_sandbox_uid` as an authorization
  precondition. `command_id` is the idempotency key across every command RPC.
- **Watching**: `WatchDispatches` sends the current Dispatches and retained deletion receipts in scope,
  ordered by version, then a `CaughtUp` checkpoint. It replays versions after the request's opaque,
  filter-bound `resume_token`; every response carries a token, and `CaughtUp` may recur after later
  changes. A checkpoint advances even when a change is outside the filter. Tokens carry no caller
  identity, so every request is authorized normally. Reusing a token with a different filter returns
  `INVALID_ARGUMENT`; a token older than retained coverage returns `OUT_OF_RANGE` with resynchronization
  detail. `DispatchFilter.command_ids` (at most 1000) narrows either scope to those commands. Tombstones
  are used for watch snapshots and replay; `GetDispatch` and `ListDispatches` continue to read current rows.
  The App persists a cursor with its applied local receipts; `session_client` carries one in the input
  observation token. Both clear expired cursors and resync, while preserving an explicit coverage-loss
  warning or source error. Resync cannot recover a pruned deletion.
- **Browser access**: the app serves an authenticated HTTP/JSON facade and calls the Dispatcher over native gRPC.
- **Delivery states** say where a command is: `DELIVERY_STATE_QUEUED_IN_DISPATCHER`,
  `DELIVERY_STATE_DELIVERING_TO_RUNNER`, `DELIVERY_STATE_RUNNER_ADMITTED`,
  `DELIVERY_STATE_FAILED`, `DELIVERY_STATE_CANCELLED_IN_DISPATCHER`, and
  `DELIVERY_STATE_UNKNOWN` (an unconfirmed effect or a deleted Sandbox's command).
  The `reason` describes queue or terminal state; `delivery_blocker` and `wake_assessment` separately
  expose the current environment blocker and latest typed wake observation. A denied or disabled wake
  leaves delivery queued.
- **Order**: commands, including `InterruptTurn`, are delivered first in, first out per Session.
  The Runtime checks the interrupt’s exact target at admission; it never interrupts a successor turn.
  A delayed or uncertain delivery follows the same receipt and recovery rules as other commands.
- **Creating a Session**: `CreateSession` durably commits the logical Session using the caller-minted
  canonical lowercase UUID `session_id` and returns a minimal `Session` resource. Its `placement`
  projects the shared `SessionPlacement` in `SessionSpec.placement`; the read projection does not
  expose SessionSpec or instructions. `status` reports owner materialization, not harness readiness
  ([contract](SPEC.md#session-resource)).
  `environment_hosted` carries one exact environment and the native Claude/Codex harness selection;
  `central_sampler_v1` carries one exact `executor_environment`; its identity, admission, notification,
  wake and deletion behavior stay Sandbox-backed ([binding rules](SPEC.md#session-binding)). Both
  placements resolve the named environment to the current Sandbox and reject a same-name replacement
  with `FAILED_PRECONDITION` before storing anything. The request is keyed by the caller-minted
  canonical lowercase UUID `session_id`.
  An exact retry uses retained Session-read authority and returns the same Session without reading live
  Sandbox or execution-account inventory. It compares the same immutable placement, exact target, spec and
  optional `setup_script` as transactional creation; a mismatch is `ALREADY_EXISTS` only for an authorized
  caller. Unauthorized retries return `NOT_FOUND` before comparing the request. A retry never updates
  attribution, binds an identity, wakes, requeues or materializes work, including failed or uncertain
  Sessions. If the row has been cleaned up, ordinary fresh-admission checks apply; the retry is not evidence
  that it still exists. Once the Sandbox is `Ready` the placement owner's `CreateSession` receives
  the logical Session before any of its commands; a central sampler Session bypasses executor
  readiness. The separate streaming `Executor.OpenSession` handshake opens an execution connection
  when needed; it is not the logical Session creation call. A
  matching runner retry returns its durable Session reservation; native preparation waits for work.
  A reservation does not assert a running harness; `ALREADY_EXISTS` means a requested spec or
  setup conflicts, which the Dispatcher records as failed Session materialization.
  Native creation rejection and unconfirmed owner outcomes fence dependent queued commands; see
  [the outcome contract](SPEC.md#opening-outcomes-and-dependent-commands).
  Session records follow the deletion and retention rules below.
- **Discovering Sessions**: `ListSessions` returns one bounded
  page of current Session records, including waiting and failed materialization before
  any Runtime or History record exists. It uses the same retained-owner read scope as
  `GetSession`, without contacting a live environment or History. `limit` defaults to 50 and
  is capped at 100; `page_token` continues in ascending Session UUID order. A cursor grants no
  authority. Pages are current reads, not a frozen snapshot: concurrent creation before the cursor
  is seen on a fresh listing. Session rows remain after Environment deletion. This includes today's
  environment-bound central Sessions without defining environment-free
  central Sessions. The CLI exposes it as `agentplane session-dispatcher list`;
  `agentplane session-dispatcher create` creates one. The `sessions/get` Action reads the Session
  alongside its independent History summary.
- **Watching Sessions**: `WatchSessions` streams the same authorized records, including Sessions
  History has not recorded, then `CaughtUp` and changed current records. Its opaque `resume_token`
  binds the version to the effective read scope; another scope is `INVALID_ARGUMENT`. Retained
  rows allow tokens to resume without expiry. A PostgreSQL listener reconnect replays a full
  snapshot. The generated JSON route is `GET /v1/sessions:watch`; see the
  [watch contract](SPEC.md#session-watch). Materialization and required-Environment state do not
  assert running or idle status.
- **Caller authority**: live workload Identity SVIDs retain exact Identity ownership checks; configured
  service SVIDs carry the separate component role. Typed service delegation preserves the effective
  Identity independently of its service actor. Kubernetes bearers grant no admission. Historical
  attribution remains readable, and retries never rewrite it.
- **Create before Ready**: configured component callers preserve existing admission and deferred
  execution-Identity binding behavior. Delivery still rechecks the live target and exact ownership;
  receiver retirement does not adopt unowned rows or change attachment cardinality.
- **Queue**: no TTL; at most `max_queued_per_session` queued commands per Session, then
  `RESOURCE_EXHAUSTED`. A command for a Sandbox that does not exist is refused `NOT_FOUND` and stores nothing.
- **Sandbox deleted** (seen on the Sandbox watch): queued commands end `FAILED` (`sandbox_deleted`), delivering
  ones and admitted inputs `UNKNOWN`. Deletion writes receipt tombstones transactionally. A versioned
  `WatchDispatches` snapshot or replay reads those tombstones in version order and applies the same retained
  Session read scope as live rows. A tombstone contains no Sandbox name or UID.
- **Deletion admission fence**: cleanup retains an exact-UID tombstone in Dispatcher PostgreSQL.
  Later Session creation requests and queued commands cannot recreate deleted state, even from
  another replica's stale inventory. Admissions that commit before cleanup are included in cleanup.
  This starts at Dispatcher cleanup, not the Kubernetes deletion instant; see
  [the deletion contract](SPEC.md#deletion-admission-fence).
- **Retention**: a Sandbox's Dispatches are kept as long as it exists. Other policies may come later.
- **Delivery leases**: each head step acquires its Session's PostgreSQL lease before wake or
  materialization, renews it while the step runs, and releases it after settlement or a persisted
  deferral. The database clock decides expiry; each acquisition has a fresh holder UUID. Every
  worker write locks and checks that live token. A successor requeues only that Session's
  in-flight work and preserves `delivery_attempted` and command IDs. The lease key needs no
  Environment. Current Session creation still requires an Environment.
  No replica owns a Sandbox or queue. Environment deletion serializes with
  worker attempts and settlements through the deletion advisory lock and Session lease rows.
  Why a Session key and a timed lease: [decision record](../../docs/adr_session_delivery_lease.md).
- **Runner statuses**: `ABORTED` (quiescing) returns the command to the queue until the Sandbox has a new
  incarnation; `UNAVAILABLE` and deadline retry with backoff;
  other command errors are `FAILED` with the runner's status. The runner deduplicates redelivery on `command_id`.
  Native Session materialization uses the separate outcome contract above; an arbitrary error is not
  proof of rejection.
- **Automatic wake**: `wakeOnSend` opts a Sandbox into wake attempts; it is not a permission grant.
  Before each automatic attempt, the shared lifecycle service checks the live target UID and the
  row's retained caller or configured service broker. Action-attributed rows use their Actions
  service actor's Dispatcher broker grant; the effective Identity remains stored for Session
  authority. Admission-time eligibility does not grant wake authority. A `SuspendedBy=Requested`
  stop stays sticky for ordinary sends and automatic wakes. Interrupts follow the same wake rules as
  other queued commands.

  Each queued Dispatch and pending Session creation stores a typed `delivery_blocker` and `wake_assessment`.
  The receipt remains `QUEUED_IN_DISPATCHER` through disabled, denied, rate-limited, failed and
  uncertain wake outcomes; those observations never become command failures. `observed_at`, target
  generation, optional retry time and structured status details describe the latest decision. A
  rate-limited result is retried at its shared-budget `retry_at`. An uncertain patch response is not
  repeated for the same observed generation; the worker reassesses after that generation changes.
  The lifecycle service and PostgreSQL budget adapter share a limit of 10 distinct guarded wake
  generations per caller principal in a rolling 60-second window. ServiceAccounts use their exact
  UID, Identities use their namespace/name/UID, and services use their SPIFFE ID; every reservation
  is scoped by target Sandbox UID and generation. Retries of a generation reuse its reservation. After a successful wake
  request, delivery waits for the Sandbox to become `Ready`; if it remains unready for ten minutes,
  the existing queue deadline fails the work with the current Ready condition details. A central
  sampler Session bypasses execution-Sandbox readiness and wake so its input remains deliverable
  while the execution environment is suspended.

Delivery enqueues the affected Session on a committed command or Session change (keyed PostgreSQL
`NOTIFY`), a Sandbox informer event, or a per-Session retry deadline. Startup and listener reconnect
resync pending and in-flight Sessions; there is no periodic delivery rescan. A held lease schedules
that Session at expiry; transient claim errors use exponential backoff. Active steps renew their
Session lease every ten seconds; a lease lasts thirty seconds. See the
[recovery contract](SPEC.md#delivery-scheduling-and-recovery).

## Deviations and gaps

- A Dispatcher admission proves only that the Runner journaled the command. History owns command outcomes,
  including its separate `no_op` outcome.
- Sandbox Pods keep their proxy-only egress. The relay reaches the shared TLS gRPC listener through
  central CONNECT and authenticates with its Runner SVID; the configured CLI credential string remains
  inert. Dispatcher certificate admission and exact service grants remain unchanged. A concrete
  MCP/client caller and History provenance propagation remain outside this transport change.

The detailed delivery and transport contract is in [SPEC.md](SPEC.md).

## Running

Build the serving binary at `//agentplane/session_dispatcher/cmd:runner-gateway` or the
Sessions image at `//agentplane/session_dispatcher:image`. See the
[Sessions settings](../README.md#source-and-tests) for TLS and caller grants. The binary uses
`AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL` to apply the embedded Goose migrations before
opening the listener. Run the same binary with `--migrate-only` for explicit fixture or
maintenance use. The startup probe waits for initialization before `grpc.health.v1` readiness.

Automatic wake execution and its budget contract live in [`wake`](wake/manager.go). Environments
owns explicit resume; both use the shared [`lifecycle` policy and guarded writes](../../environments/lifecycle/SPEC.md).
Only automatic wake reserves the shared PostgreSQL budget, after current authorization and before
an optimistic patch; an uncertain patch outcome is never replayed.
