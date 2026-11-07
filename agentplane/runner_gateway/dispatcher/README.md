# Session Dispatcher

The durable command queue is the Dispatcher role of the
[Runner Gateway](../README.md). The Go integration app and the Notification Service use the
Session Dispatcher gRPC contract; the stable Dispatcher Service maps its port `8080`
to the shared listener on `8080`. The credential-holding Sandbox relay presents a Runner SVID to
the dedicated workload listener on `8082`, which serves gRPC and generated HTTP/JSON under its
explicit authentication allowlist; the deployed Gateway allows SPIFFE only. The worker delivers
through the Runner proxy once
the exact Sandbox is ready, with per-Sandbox Lease and PostgreSQL generation fencing.

The manual queue, replica and wake probes are documented in the
[system tests](../../system_tests/README.md#dispatcher-queue-and-replicas).

## Contract

- **One RPC per command**: `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, and `InterruptTurn`.
  `CreateSession`, `GetSession`, and `ListSessions` create and read logical Session records; the
  other Session RPCs operate on them. `GetDispatch`, `ListDispatches`, `WatchDispatches`, and
  `CancelDispatch` read or cancel command receipts
  ([proto](session_dispatcher.proto)). Commands name the Session; Dispatcher resolves its placement.
  A broker with an incarnation-scoped grant supplies `expected_sandbox_uid` as an authorization
  precondition. `command_id` is the idempotency key across every command RPC.
- **Watching**: `WatchDispatches` sends the Dispatches in scope, then one `CaughtUp` frame, then a Dispatch
  again whenever it changes. `DispatchFilter.command_ids` (at most 1000) narrows either scope to those
  commands, so a caller holding a few of them does not replay a whole Session.
- **Browser access**: the app serves an authenticated HTTP/JSON facade and calls the Gateway over gRPC.
- **Delivery states** say where a command is: `DELIVERY_STATE_QUEUED_IN_DISPATCHER`,
  `DELIVERY_STATE_DELIVERING_TO_RUNNER`, `DELIVERY_STATE_RUNNER_ADMITTED`,
  `DELIVERY_STATE_HARNESS_CONFIRMED`, `DELIVERY_STATE_NO_OP`, `DELIVERY_STATE_FAILED`,
  `DELIVERY_STATE_CANCELLED_IN_DISPATCHER`, and `DELIVERY_STATE_UNKNOWN` (an unconfirmed effect or a
  deleted Sandbox's command).
  The `reason` describes queue or terminal state; `delivery_blocker` and `wake_assessment` separately
  expose the current environment blocker and latest typed wake observation. A denied or disabled wake
  leaves delivery queued.
- **Order**: commands, including `InterruptTurn`, are delivered first in, first out per Session.
  The Runtime checks the interrupt’s exact target at admission; it never interrupts a successor turn.
  A delayed or uncertain delivery follows the same receipt and recovery rules as other commands.
- **Creating a Session**: `CreateSession` durably commits the logical Session using the caller-minted
  canonical lowercase UUID `session_id` and returns a minimal `Session` resource. Its `placement`
  projects the shared `SessionPlacement` in `SessionSpec.placement`; the read projection does not
  expose SessionSpec or instructions. `status` reports owner materialization using
  `SESSION_MATERIALIZATION_WAITING`, `SESSION_MATERIALIZATION_IN_PROGRESS`,
  `SESSION_MATERIALIZATION_ACCEPTED`, `SESSION_MATERIALIZATION_FAILED` or
  `SESSION_MATERIALIZATION_UNKNOWN`, with `reason`, `delivery_blocker` and `wake_assessment` where
  available. This is owner materialization state, not harness readiness.
  `SESSION_MATERIALIZATION_ACCEPTED` means the placement owner durably admitted the Session; it does
  not mean a native harness is ready, a model turn ran, or History projected a Thread.
  `environment_hosted` carries one exact environment and the native Claude/Codex harness selection;
  `central_sampler_v1` carries one exact `executor_environment`. Central sampler V1 still uses the
  existing Sandbox-backed identity, admission, notification, wake and deletion behavior. Its
  executor environment is fixed for the Session; it is not an optional attachment, and the central
  Session cannot yet open without an environment. Both placements resolve the named environment to
  the current Sandbox and reject a same-name replacement with `FAILED_PRECONDITION` before storing
  anything. The request is keyed by the caller-minted canonical lowercase UUID `session_id`.
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
  [the outcome contract](SPEC.md#native-opening-outcomes-and-dependent-commands).
  Session records follow the deletion and retention rules below.
- **Discovering Sessions**: `ListSessions` (`GET /v1/sessions`) returns one bounded
  page of current Session records, including waiting and failed materialization before
  any Runtime or History record exists. It uses the same retained-owner read scope as
  `GetSession`, without contacting a live environment or History. `limit` defaults to 50 and
  is capped at 100; `page_token` continues in ascending Session UUID order. A cursor grants no
  authority and remains usable if its last record was deleted. Pages are current reads, not a frozen
  snapshot: concurrent creation before the cursor is seen on a fresh listing, and cleanup removes
  records. This includes today's environment-bound central Sessions without defining environment-free
  central Sessions. The CLI exposes it as `agentplane sessions list`; `agentplane sessions create`
  creates one and `agentplane sessions get` reads the Session alongside its independent History
  summary.
- **Wire and data cutover**: this cutover moves placement into the `SessionSpec` oneof; the flat
  harness field and Dispatcher-level placement fields are retired. Coordinate the Dispatcher,
  sampler, app/CLI and other logical Session callers with the matching environment/runtime images.
  The logical creation/read APIs are `CreateSession`, `GetSession` and `ListSessions` across Dispatcher,
  App, Environment Runtime and central Sampler; old opening RPCs and resource projections have no
  compatibility route. The Runner's distinct streaming `Executor.OpenSession` remains the transport
  that opens an execution connection. Old callers and old Sandbox binaries are not supported in an
  overlap window. The in-place migrations convert retained session and sampler request records and
  preserve Session/History data; they do not requeue commands or replay effects. See the
  [placement cutover details](SPEC.md#sessionspec-placement-cutover).
- **Caller authority**: a normal workload token must prove the target Sandbox's exact controller-owned
  execution ServiceAccount UID. New Sessions retain that UID as their immutable logical principal, and
  retain the actual opening caller separately as creator/broker. Normal command writes, cancellation,
  and interrupt also require the recorded principal to match the live managed account UID. A queued
  delivery rechecks that UID immediately before calling the Runner. Normal receipt reads and watches
  filter by the retained UID and do not require a live Sandbox or account. The configured backend
  accounts keep their existing privileged behavior only when TokenReview also proves a Pod binding;
  an account-only token for the same name uses normal per-Session authority.
- **Create before Ready**: a configured Pod-bound backend can create a Session before the controller
  has created the execution ServiceAccount. The Session record stores the expected target account with
  its UID pending. Delivery binds that UID only after confirming the account has a controller OwnerReference
  to the exact Sandbox UID. Legacy Session records with no recorded owner remain unowned by normal callers and
  are never adopted based on a matching current account name.
- **Queue**: no TTL; at most `max_queued_per_session` queued commands per Session, then
  `RESOURCE_EXHAUSTED`. A command for a Sandbox that does not exist is refused `NOT_FOUND` and stores nothing.
- **Sandbox deleted** (seen on the Sandbox watch): queued commands end `FAILED` (`sandbox_deleted`), delivering
  ones and admitted inputs `UNKNOWN`. The Dispatcher publishes their exact protobuf snapshots through
  transactional PostgreSQL notifications in bounded chunks, in the same transaction that deletes the rows,
  so active `WatchDispatches` streams on any connected replica receive them. A listener disconnect, reconnect
  gap or incomplete event fails active streams with `UNAVAILABLE`; terminal events have no durable replay,
  so a later read or watch is `NOT_FOUND`/empty.
- **Deletion admission fence**: cleanup retains an exact-UID tombstone in Dispatcher PostgreSQL.
  Later Session creation requests, queued commands and ownership claims cannot recreate deleted state, even from
  another replica's stale inventory. Admissions that commit before cleanup are included in cleanup.
  This starts at Dispatcher cleanup, not the Kubernetes deletion instant; see
  [the deletion contract](SPEC.md#deletion-admission-fence).
- **Retention**: a Sandbox's Dispatches are kept as long as it exists. Other policies may come later.
- **Replica ownership**: a per-Sandbox Lease assigns delivery scheduling ownership while the Sandbox
  has a `QUEUED_IN_DISPATCHER` or `DELIVERING_TO_RUNNER` Dispatch or Session creation. Its PostgreSQL
  generation fences stale claims, settlements and recovery. A Sandbox without such work is no longer
  renewed and its Lease lapses; the next work reacquires it. The Runner Gateway's separate connection
  Lease still owns the runner stream; Dispatcher scheduling does not affect it. A new owner requeues
  only that Sandbox's in-flight work, preserving at-least-once redelivery with the same command IDs.
- **Runner statuses**: `ABORTED` (quiescing) returns the command to the queue until the Sandbox has a new
  incarnation; `UNAVAILABLE` and deadline retry with backoff;
  other command errors are `FAILED` with the runner's status. The runner deduplicates redelivery on `command_id`.
  Native Session materialization uses the separate outcome contract above; an arbitrary error is not
  proof of rejection.
- **Automatic wake**: `wakeOnSend` opts a Sandbox into wake attempts; it is not a permission grant.
  Before each automatic attempt, the shared lifecycle service checks the live target UID, current
  execution ServiceAccount UID and `resumePolicy`, or the separately configured trusted wake broker
  grant against the row's retained effective sender. Dispatcher actor identity and admission-time
  eligibility do not grant wake authority. A `SuspendedBy=Requested` stop stays sticky for ordinary
  sends and automatic wakes. Interrupts follow the same wake rules as other queued commands.

  Each queued Dispatch and pending Session creation stores a typed `delivery_blocker` and `wake_assessment`.
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
- Sandbox Pods keep their proxy-only egress and existing CLI URL. The relay maps the exact Dispatcher
  authorities to the workload TLS listener through central CONNECT and authenticates with its Runner
  SVID; the configured CLI credential string remains inert. Backend TokenReview listeners and their
  caller allowlists are unchanged. A concrete MCP/client caller and History provenance propagation
  remain outside this transport change.

The detailed ownership, compatibility and transport contract is in [SPEC.md](SPEC.md).

## Running

The serving binary and image live under `//agentplane/runner_gateway` and
`//agentplane/runner_gateway:image`. It reads the audience and caller settings described in
<../README.md>, a projected Gateway token for calls to other holder replicas, and
`AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL`. The serving binary applies the embedded Goose queue migrations before opening the Gateway
listener; the same binary accepts `--migrate-only` for explicit fixture or maintenance use. The
startup probe waits for initialization before `grpc.health.v1` readiness on the shared listener.
