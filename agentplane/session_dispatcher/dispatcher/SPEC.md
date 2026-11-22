# Session Dispatcher caller authority

This contract governs SessionDispatcher gRPC methods served by Sessions and the generated
HTTP/JSON facade served by the App.
It scopes existing Sessions and existing environments; it does not create environments or select templates.

## Process bootstrap

The Sessions process loads strict YAML settings from `AGENTPLANE_SESSIONS_CONFIG_FILE`.
The database URL comes from the explicit runtime Secret input
`AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL`.

## Caller proof

- The TLS listener at `sessions:8080` serves Dispatcher and gRPC health. Dispatcher admits
  live workload Identity SVIDs, configured exact service SVIDs and configured typed service
  delegation. Environments serves EnvironmentService, Runtime proxy, HarnessHost and Executor
  on its own listener.
- Workload traffic reaches the shared TLS listener through central-proxy CONNECT and presents its
  own SVID. Live credential and Identity UID resolution accepts standing credentials with no
  Sandbox or Pod, so no Environment attachment is required.
- Request fields never assert caller identity. A service principal carries no fabricated account
  UID or Pod, and certificate-plus-bearer authentication is refused. Retained ServiceAccount
  attribution and historical Session records remain readable without admitting new bearer calls.
- Creator/broker identity is retained for provenance, not Session ownership. The exact Identity
  creator may recover only its matching `CreateSession` receipt; that narrow retry authority does not
  grant `GetSession`, command, cancellation or watch access to the resulting Session.

## Service certificate callers

`dispatcher_service_callers` grants exact service SPIFFE IDs the component Dispatcher role.
These callers have their own typed principal and attribution; no account UID or Pod is fabricated.
`dispatcher_service_delegation` independently grants exact certificate actors and exact Dispatcher
RPCs typed Identity delegation. It requires Action provenance and a fresh effective Identity UID.
Delegated actors never inherit the direct component role. Action-attributed rows use their service
actor's independent wake grant while keeping the effective Identity in attribution. Direct services
require a separate service wake grant.
Both allowlists default empty and are configured only after receiver image pins.

Additive migration 19 retains service actor, optional effective Identity and Action correlation in
separate service attribution columns. Service wake attempts use their own budget table keyed by SPIFFE ID, never
an account-shaped synthetic key. Old binaries see new actor attribution as absent, so rollback to
an old receiver must precede caller enablement or explicitly assess pending service-attributed work.

## Delegated callers

- `dispatcher_service_delegation` names exact service actors and exact Dispatcher RPC methods.
  Middleware verifies the actor's SVID, typed Action provenance and the effective Identity's live
  UID before handlers run.
- Delegated calls use ordinary Identity Session ownership and `bind_session` checks. Actor grants
  never confer component privileges on the effective Identity. The Action actor's service broker
  grant applies only to automatic wake and does not lend component authority to that Identity.
  Native Session creation still resolves the selected Sandbox's live execution Identity, which may
  differ from the creator only under the existing explicit policy grant.
- Durable openings and dispatch rows retain actor, effective caller and Action correlation
  separately. Request and execution UUIDs are correlation only; Session/command IDs and domain
  request equality retain their reconciliation meaning. Retries cannot rewrite admission evidence.
- Wake eligibility ordinarily uses the effective Identity. A row carrying Action proof uses its
  verified service actor's independent exact service grant; the effective Identity remains in row
  attribution. No credential or Action lease token is persisted, and Dispatcher does no Actions or
  App lookup.

## Session binding

- The committed Session is Dispatcher's live Session reservation and placement authority. Its
  immutable `SessionSpec.placement` is a shared `SessionPlacement` message whose oneof is the
  single source of backend and placement. The
  `environment_hosted` variant contains one exact environment and the native Claude/Codex harness
  selection. The `central_sampler_v1` variant contains one exact `executor_environment`. Dispatcher
  validates and routes from the stored spec; `Session.placement` projects that same message under
  retained-owner policy without exposing the full spec or instructions. Native harness store identity and harness-native ID
  are not represented by this cutover and remain future work.
- Central sampler V1 keeps today's Sandbox-backed identity, admission, notification and wake
  semantics. Its executor environment is required and fixed for the Session. This is not an optional
  attachment API, and it does not permit a central Session to open without an environment.
- `CreateSession` requires `session_id` to be a canonical lowercase UUID before reading Sandbox state or recording a
  Session. Exact retries and authorized reads still find the record after its required Environment disappears.
  Explicit Session deletion is separate and not yet implemented; callers must not deliberately reuse a Session UUID
  after explicit deletion, and retained History can reject conflicting reuse.
- A configured service caller keeps its privileged `CreateSession` path. An Identity caller may
  create only an `environment_hosted` Session and must have a current `bind_session` grant for the exact live
  Sandbox named in the placement. Central placement, an unbound Sandbox, and a target with a pending deletion or
  operator-requested stop remain refused for Identity creation.
- An existing `CreateSession` request is a read-only idempotent retry. Before comparing its immutable request,
  Dispatcher checks the same retained owner UID or configured service-caller authority as receipt reads; an
  Identity creator can recover its receipt only when the retained creator Identity name and UID match. An
  unauthorized caller gets `NOT_FOUND` without conflict details. The comparison is shared with
  transactional creation and includes exact environment name/UID, deterministic spec bytes, and optional
  setup bytes. Matching retries do not consult live environment inventory or change materialization,
  attribution or wake state. Delegated calls still pass middleware's current
  effective-identity validation and never inherit their actor's privilege. An absent row still requires
  normal fresh admission; this path provides no historical deletion registry.
- Every new opening retains the target execution Identity as its immutable logical principal and stores the
  authenticated creator separately. Ordinary Session reads continue to follow execution ownership; receipt
  recovery follows only the exact creator check above.
- Dispatcher is the authority for the Identity a Session executes as. Every new opening records the
  Identity its Sandbox names in `spec.identity`, read live with its UID, beside any execution ServiceAccount;
  a Sandbox naming an absent Identity cannot open a Session (`FAILED_PRECONDITION`). A Sandbox naming an
  Identity whose managed ServiceAccount is absent opens Identity-run Sessions, which record the Identity and
  no ServiceAccount: only privileged and Identity callers open them, and placement and delivery check only the
  Identity. Dispatcher
  passes both to the runtime or central sampler when it places the Session. Placement and command delivery
  require the Sandbox to still name that exact Identity incarnation, or none for a Session recorded with none;
  otherwise the work fails `execution_identity_changed`. A stopped Sandbox can change its Identity while Sessions
  are retained. Session admission, placement and every command delivery recheck the live Identity;
  retained Sessions keep their recorded Identity and fail their next command after a rebind.
  `Session.identity` reads that Identity back (absent when the Sandbox named none), beside
  `Session.execution_identity`, the execution ServiceAccount.
- An Identity caller (a workload SVID naming an Identity, admitted under the configured
  `policy_namespace`) is never privileged. A native `CreateSession` requires an explicit `bind_session`
  grant even on a Sandbox acting as the caller; acting as the Identity alone does not authorize lifecycle control.
  It reads, writes, cancels and watches only Sessions whose recorded execution
  Identity is that incarnation, never one recorded with only a ServiceAccount; writes and cancellation also
  require the Sandbox to still name it. Its rows record the Identity and its Pod as creator or actor, in
  columns separate from ServiceAccount attribution. Its queued work requests an automatic wake as the Identity,
  which the lifecycle manager authorizes exactly as the Sandbox's own ServiceAccount's: the target Sandbox
  must act as that live Identity incarnation and its `resumePolicy.allowOwnServiceAccount` must permit its
  own principal to resume it. Messaging between Sessions of one Identity, and the wake it causes, is implied;
  a direct resume or wake request by an Identity still has no rule type and is refused.

## Method scope

- SubmitInput, ChangeModel, ChangeReasoningEffort and InterruptTurn require an existing Dispatcher Session
  record for every caller, including configured service callers. The supplied Sandbox name and UID must
  match that record. Missing records and records hidden from the caller return `NOT_FOUND`, before command
  conflict details. No Session record is reconstructed from Runtime journals or History.
- Normal Session commands and CancelDispatch require the caller to match
  the opening's execution Identity and Sandbox UID. Normal calls also require the Sandbox to still name that
  Identity incarnation; a changed or missing Identity cannot authorize a write.
- GetSession, ListSessions, WatchSessions, GetDispatch and ListDispatches filter retained rows by the recorded execution Identity.
  These reads do not require a live Sandbox or Identity. Old rows without a trustworthy owner are hidden
  from Identity callers but remain visible to service callers.
- GetDispatch includes the full typed command as stored by Dispatcher. Command mutation responses omit the
  field so older ProtoJSON clients can still decode their receipts.
  ListDispatches and WatchDispatches include it when the filter sets `include_command` and names 1–16 distinct
  command IDs. Other filters return receipt metadata and any existing admission evidence. A legacy deletion receipt
  has no stored command. Presence of a command says nothing about Runner admission.
- WatchDispatches applies the retained Session read scope to live rows and deletion receipts, authorizes
  every request, and resumes from an opaque filter-bound version token. The token carries no caller identity;
  a filter mismatch returns `INVALID_ARGUMENT`, and a cursor behind retained coverage returns `OUT_OF_RANGE`.
- Commands name the Session. Dispatcher resolves the exact placement from its Session record. A broker
  whose grant is scoped to an incarnation supplies `expected_sandbox_uid`; a mismatch is denied before
  admission or forwarding. The UID is a constraint on authority, not a placement chosen by the caller.
- Queued-command admissions recheck caller, Session existence and target Sandbox binding in the same
  PostgreSQL transaction that appends work, including internal admissions and privileged backends. A row lock
  prevents Session deletion or owner changes from committing between that check and admission. Exact command
  retries still return the original receipt after authorization, including terminal opening outcomes.
  Cancellation keeps its existing retained-receipt authority, including privileged backend access.
  Interrupts use this same durable admission, delivery and cancellation path, with the target turn ID
  checked by the Runtime at admission. A delayed interrupt cannot target a successor turn.
- The worker checks the live execution Identity before placement and again
  immediately before Runner delivery. Source errors fail closed and retry, while
  a changed Identity fails queued work instead of delivering it under the replacement.
- The delivery router requires the Session record and checks its stored Sandbox name and UID for
  native and central Sessions before selecting the backend. A missing record never falls back to
  a native Runtime. This check is not an atomic fence against deletion after the read; managed
  quiescence and Runtime's admission barrier remain separate.
- Automatic wake is attempted only when the live Sandbox has `spec.wakeOnSend: true`. The shared lifecycle service
  rechecks the target UID and retained caller: for an Identity caller, that the Sandbox acts as that live
  Identity incarnation under its current `resumePolicy`; a retained ServiceAccount caller is never authorized.
  `wake_service_callers` is an automatic-only broker grant. An Action-attributed row is assessed
  as its verified service actor through the Dispatcher's exact service broker grant; its effective Identity
  remains in the row attribution for Session authority. These grants do not authorize explicit resume or
  override `SuspendedBy=Requested`. Admission-time eligibility never grants wake authority.
- A queued opening denied wake for its creator does not suppress an authorized queued input or unattended
  notification. Dispatcher assesses downstream wake under each command's own retained effective identity,
  without forwarding commands ahead of the opening. Only a proven caller-authorization denial permits trying
  another command's wake authority. Requested and uncertain wake outcomes, and an unexpired rate limit,
  block other downstream wake attempts while the opening remains queued, including after cancellation or
  process restart. A wake call's uncertainty is persisted before calling lifecycle; cancellation retains it.
  A later observed generation permits reassessing an uncertain wake under the existing generation rule.
- `Dispatch` and `Session` keep materialization state separate from wake state. `Session.status.materialization`
  maps queued, delivering, admitted, failed and unknown outcomes to
  `SESSION_MATERIALIZATION_WAITING`, `SESSION_MATERIALIZATION_IN_PROGRESS`,
  `SESSION_MATERIALIZATION_ACCEPTED`, `SESSION_MATERIALIZATION_FAILED` and
  `SESSION_MATERIALIZATION_UNKNOWN`; `reason`, `delivery_blocker` and the typed
  `wake_assessment` are persisted with the receipt and published transactionally to active watches, including updates
  that leave delivery state and reason unchanged. Disabled, denied, rate-limited, failed and uncertain wake outcomes
  remain queued; only the existing post-request readiness deadline can terminally fail work. The assessment records
  its observation time, observed generation, optional retry time and structured status details. The shared lifecycle
  budget charges caller namespace/name/UID plus Sandbox UID and generation. An uncertain patch result is not retried
  during the same observed generation; a later generation triggers a fresh assessment.
- Both placements use one HarnessHost client for creation and the four commands. Central delivery
  selects the configured sampler target; hosted delivery selects Environments and sends the exact
  Sandbox routing metadata. Both present the Sessions service SVID.
- Central sampler openings and their inputs bypass executor readiness and automatic wake. The suspended execution
  environment does not block delivery to the central sampler.

## Transport boundary

Sandbox Pods retain proxy-only egress. Their credential-holding relay reaches the shared TLS gRPC
listener through central CONNECT; the listener authenticates the Runner SVID before applying the
Dispatcher method rules. EnvironmentService has its own listener. The configured CLI credential string
remains inert for compatibility. Backend callers use the same listener with the configured
Dispatcher certificate admission and exact grants.

## Typed delivery observations

Dispatch and Session-opening receipts carry an optional `delivery_blocker` and a typed
`wake_assessment`, independently of their delivery state. These fields round-trip through durable
storage, Get/List/Watch and `sessions__observe_input`. A changed assessment is a watch update even
when delivery state and reason are unchanged. An absent stored assessment reads as
`WAKE_STATE_NOT_EVALUATED`; this does not claim a wake request was authorized or attempted.

Automatic wake is assessed and requested through the shared lifecycle manager by the Sessions
binary's in-process DeliveryWorker. The worker preserves row attribution and records the manager's typed
assessment; Action proof selects the actor's service broker grant for wake. See the lifecycle manager
SPEC for its policy and mutation contract.

## Deletion admission fence

Once Dispatcher cleanup for an exact Sandbox UID commits, later `CreateSession` and queued-command
admissions for that UID fail with `FAILED_PRECONDITION`, including privileged callers using an
inventory snapshot read before cleanup. The durable UID fence survives process restart. A same-name environment with a different UID has independent admission.

Cleanup retains Sessions with their immutable placement and execution principal. It records that
the required Environment is deleted separately from owner materialization: a not-yet-sent opening
becomes `FAILED`, an opening in progress becomes `UNKNOWN`, and accepted or already-terminal
materialization keeps its evidence. `GetSession`, `ListSessions` and exact `CreateSession` retries
expose `SessionStatus.required_environment=DELETED` to authorized callers. No command can be
admitted or routed through the deleted UID. Retained Session rows are excluded from the cleanup
inventory so reconciliation does not repeat the deletion indefinitely.

Admission transactions and cleanup serialize per Sandbox UID. Admission that commits first is
included in cleanup; cleanup that commits first prevents admission. Existing terminal dispatch
notifications remain in the cleanup transaction. This fence neither cancels already issued native
effects nor replays them, and it does not delete native stores, sampler Sessions or retained History.

Cleanup collects candidate UIDs from stored work absent from the synchronized inventory. It then
confirms absence with a complete uncached Kubernetes API read, after fixing the candidate set: a
new live environment omitted by the earlier snapshot cannot be permanently fenced. Failure to
confirm absence aborts cleanup. This is not an atomic commit with Kubernetes deletion: if a removed environment has no stored work, a first admission
using stale inventory can still commit before cleanup observes its row. The next reconciliation
removes it and closes admission permanently. During a rolling upgrade, the fence applies only after
all admitting/cleanup Sessions replicas use this protocol; the additive migration alone does not
fence old writers.

## Opening outcomes and dependent commands

The Dispatcher retains logical existence and the immutable creation request after its placement owner
rejects materialization. An exact opening retry reads that receipt; changing configuration requires
a new Session UUID. Placement-owner admission reserves a Session, not a running harness.

A native Runtime `CreateSession` response of `INVALID_ARGUMENT` proves validation rejected the request before
reservation; `ALREADY_EXISTS` reports a conflicting retained request. Dispatcher records these as
`SESSION_MATERIALIZATION_FAILED` and, in the same transaction, fails commands still `QUEUED_IN_DISPATCHER` and never forwarded with
`session_rejected`; a previously attempted command becomes `UNKNOWN`. It does not alter delivering or admitted commands. Admission and
settlement serialize on the Session ID, and settlement checks the current Session lease holder.
A command admitted first is included; a later new queued command is refused with
`FAILED_PRECONDITION` without a stored receipt. Exact existing-command retries still return the
original receipt after ordinary caller authorization.

`ABORTED`, `UNAVAILABLE` and deadline responses keep the existing same-ID reservation reconciliation
and backoff. The placement owner recovers its durable reservation before preparation; these retries do
not authorize replay of uncertain effects. Other opening errors, including
`INTERNAL`, `UNKNOWN`, `RESOURCE_EXHAUSTED` and unproven preconditions, become `UNKNOWN`. Their
pending commands remain queued with `session_unconfirmed`; they do not wake the environment
or reach the placement owner. New queued commands are refused as above. Older `FAILED` openings lack proof of
rejection and receive the same hold without rewriting their stored outcome. Held commands remain
cancellable. Automatic resolution of those held openings is not provided.

For central openings, `INVALID_ARGUMENT` and `ALREADY_EXISTS` are also unconfirmed: the sampler
may validate current configuration before checking a reservation from an earlier timed-out attempt.
The command hold rules apply to both placements after all Dispatcher replicas run this protocol.

## Possible command forwarding and deletion outcomes

Before a queued command RPC leaves Dispatcher, its current fenced worker commits a sticky
`delivery_attempted` fact under the same deletion fence used by environment cleanup. A removed row
or stale owner cannot acquire that permission. Storage uncertainty is retried before forwarding;
cancellation or ownership loss prevents forwarding. A worker crash, requeue or owner recovery
retains the fact. Only an original command ID and payload may be reconciled; this is never permission
to replay unknown native effects or target a replacement environment.

Cancellation is available only before possible forwarding. A later identity/policy/wake failure or
bare RPC error cannot prove that a prior attempt had no effects. Unavailable/deadline replies keep
the existing same-ID journal reconciliation; other unproven terminal RPC errors become `UNKNOWN`.
An actual returned Runtime admission remains admission evidence. An admitted input can have an
unknown later execution/effect outcome on deletion; its terminal snapshot retains the admission
entry and cursor. `UNKNOWN` does not negate that proven journal acceptance. An unmarked command, including a claimed row still before the forwarding marker, can fail
as known-unsent; a requeued possibly-forwarded command becomes `UNKNOWN` on environment deletion.

Deletion cleanup commits the Session status and delivery outcomes together. The retained Session owns
the original commands and their receipts. Authorized Get, List and Watch reads continue across
Environment deletion and Dispatcher restart. During a rolling upgrade, a pre-`00029` replica may
revisit every retained Environment UID on each full reconciliation. Migration `00029` suppresses its
cleanup deletes for owned commands, so those extra scans preserve terminal outcomes and versions.
Previously deleted commands have only the receipt
evidence still present in legacy watch tombstones. Runtime journals and History retain their own evidence.

Treating an unmarked claim as known-unsent requires a cutover with no old command writers still
running; overlapping old writers do not provide this evidence. A lost marker-commit reply is retried
under ownership before any RPC. If ownership or the request is lost, no RPC is issued; a committed
marker without a send remains conservatively uncertain.
The migration adds the column without altering authored data or History. A rollback to old writers
loses the forwarding-evidence guarantee even if the column remains present.

## Paged command discovery

`ListSessionDispatches` pages one retained Session's current Dispatch rows in descending submission
position. Each page checks the Session's recorded owner and exact placement. Its metadata includes
the full delivery reason and positive database receipt revision. The ProtoJSON `receiptVersion`
is a decimal string and changes on each row update. Use `GetDispatch` for individual admission evidence. The default page
contains up to 25 rows and the maximum is 100.

The first page fixes membership for later admissions. Receipt updates read at each page's snapshot
do not change the order. The continuation token binds the position and deletion frontier to the
Session and read scope. A physical Dispatch deletion anywhere after that frontier, including one
whose tombstone was subsequently pruned, can return `OUT_OF_RANGE`. Discard the traversal and
restart on that status. An empty `next_page_token` completes the retained-row enumeration.
`coverage=RETAINED_DISPATCH_ROWS` excludes legacy tombstones, which are available only through
`WatchDispatches` until expiry. Absence cannot establish that a command was never accepted.
Older servers return `UNIMPLEMENTED` for the RPC.

To verify durable membership for known commands, pass up to 16 distinct
`verify_retained_command_ids` without pagination options. This reads the same exact Session
rows as paged discovery and checks migration 29's enabled deletion-protection trigger in
the same database snapshot. Only a returned row with `retains_commands_after_environment_deletion=true`
establishes this coverage for that command. Older responses omit the capability. Callers can compare
the separately fetched stored command with their authored bytes before releasing local storage.

## Versioned dispatch watches

Migration `00024` backfills each Dispatch with a sequence-backed version and installs database triggers
that assign a fresh version on every later insert or update, including writes from older application
images. A deletion trigger stores each receipt in `dispatch_tombstone`, keyed by command ID and version,
without Sandbox name/UID or Dispatch command/routing columns. Versions distinguish deletions if a command
ID is reused. A retained receipt may include original command content; its retained Session row supplies
ownership and read scope. A bounded pruner removes tombstones after 24 hours and advances a durable,
global pruned-through version.

`WatchDispatches` uses payload-free notifications from `postgres/wakeups.Hub` to wake versioned database
reads. A fresh snapshot includes matching current Dispatch rows and retained tombstones, ordered by version.
Incremental reads return records with a version greater than the cursor. Both apply the existing Session
read scope; a tombstone has no Sandbox name or UID to filter on directly. Token encoding and decoding use
the shared `dispatcher/internal/resumetoken` package; wakeups use `postgres/wakeups.Hub`.
When `postgres/wakeups.Hub.Generation()` changes after a listener reconnect, the receiver rescans from its current
durable cursor so commits made during the disconnect are covered.

The request and every response carry an opaque, versioned `resume_token` containing the last version and a
digest of the request filter, but no principal. Every request performs ordinary authorization. A token used
with another filter returns `INVALID_ARGUMENT`. If its cursor is below the global pruned-through version,
the server returns `OUT_OF_RANGE` with resynchronization detail. The global floor is conservative: it can
require a fresh snapshot even when the filtered scope had no deleted rows in the pruned interval.

`CaughtUp` marks the checkpoint after the initial snapshot and each incremental read; it can appear more
than once. Its token carries the read frontier even when changes at that frontier did not match the filter.
This lets unrelated writes advance a watch without causing repeated reads of the same range.

The App retains the cursor with its locally applied command receipts and effective filter. Reconnects
resume from that cursor; recurring or resumed `CaughtUp` frames do not imply any unchanged command is
missing. Only a fresh snapshot reconciles missing receipts. `OUT_OF_RANGE` clears the cursor and starts
a fresh snapshot, while a visible coverage warning remains: pruned deletion receipts cannot be recovered.
`session_client.ObserveInput` carries the Dispatch cursor inside its existing observation token. It
replays retained tombstones when `GetDispatch` no longer has a live row, and preserves an `OUT_OF_RANGE`
Dispatch source error after resync. Both clients advance only after applying a response frame; the App waits for the first `CaughtUp`
before checkpointing a fresh snapshot, so an interrupted snapshot repeats missing-row reconciliation.

**Sequence allocation is not commit order.** Before establishing a checkpoint, the reader takes brief
PostgreSQL `SHARE` locks on `dispatch` and `dispatch_tombstone`, so a writer that already allocated a lower
version commits before the cursor advances. That transaction reads only the indexed version maxima and
singleton retention floor, then commits. The batch query runs afterward with `version > after AND version <= through`
in a separate read-only MVCC snapshot, which permits dispatch writers to proceed throughout the query.
It rechecks the retention floor in the same snapshot as the receipts, so pruning between transactions
cannot silently lose a deletion. Updates or deletions after the frontier may coalesce into the next batch.
No transaction spans a stream send. Frontier transactions remain per stream: Hub wakeups coalesce per
subscriber and have no shared notification epoch suitable for safely invalidating a replica-wide cache.

Direct `GetDispatch` and `ListDispatches` read current rows. The tombstones provide watch snapshot
and replay coverage. Supported rollback images must serve versioned watches and keep migration
`00024` installed.

### Session delivery workset

`WatchDispatches` accepts `workset_session_id` in place of its ordinary filter. The workset
uses the same Session authorization and includes metadata for up to 1,024 queued or delivering
commands, 20 recent failed or unknown receipts, and 20 recent admissions or cancellations.
The last group lets the browser bridge the interval between delivery and History projection.
Each group indicates overflow. Recent receipts include legacy tombstones. A reason excerpt
carries `reason_truncated` when a command-ID-filtered ordinary watch is needed to show the
full retained reason. The ordinary watch includes legacy tombstones, including after their
live rows become unavailable. Payloads and journal entries remain available through their
existing reads. History determines command effects.
Each item includes its positive Dispatcher receipt version. A live row update and a legacy
deletion receipt each receive a distinct version. The legacy deletion flag identifies rows
held by the expiring tombstone store.

The first frame on every connection replaces the previous workset. Later replacements follow
Session-specific changes, including pruning of that Session's legacy tombstones. A PostgreSQL
listener interruption ends the stream with `UNAVAILABLE`. A resume token is scoped to the workset
and Session and detects expired tombstone coverage with `OUT_OF_RANGE`. Clearing the token
requests a new current snapshot. Omission from a workset gives no evidence that a command was
unsent. Older facades and Dispatchers reject requests that omit the ordinary filter.

## Session resource

`SessionSpec.placement` carries `environment_hosted` or `central_sampler_v1`; there is no flat harness
field and no Dispatcher-level placement or `initial_executor` field.

The logical creation and read operations are `CreateSession`, `GetSession` and `ListSessions` across
Dispatcher, App, Environment Runtime and central Sampler. `CreateSession` commits the Dispatcher
Session before owner materialization; an exact retry with the same caller-chosen ID and immutable
creation request returns that Session, and a conflicting reuse fails. Reconcile an uncertain create
by reading the same ID; never blindly replay native, provider or executor effects. The distinct
`Executor.OpenSession` stream is the execution-connection handshake, not a logical Session creation
API.

`Session` is a minimal authorized projection: `session_id`, `placement`, creation/update timestamps,
nested `SessionStatus` and the exact execution ServiceAccount identity once established. It does not
expose the immutable `SessionSpec` or instructions. A missing identity means no exact owner is
established; callers must not infer its UID from a current same-name account.
`SessionStatus.materialization` is placement-owner progress, not harness state
([values](#method-scope)): `SESSION_MATERIALIZATION_ACCEPTED` means the owner durably admitted the
logical Session, not that a harness is ready, a turn completed or History projected a Thread.

**Gotcha:** each migration baseline adopts only a database whose previous chain has completed:
Dispatcher migration 13, central sampler migration 14 and History migration 17. A database restored
from before them must run an image that still carries those migrations first.

## Session delivery lease

A head step holds one PostgreSQL lease keyed by Session ID, beginning before wake or Session
materialization and ending after settlement or a durably recorded deferral. Its expiry uses
`clock_timestamp()`; each acquisition names a fresh holder UUID. Each worker mutation fences the
live holder in the mutation transaction. Renewal cannot revive an expired or superseded holder.
No transaction stays open across an external RPC. Loss cancels local step work; an RPC already
in flight can still arrive, so Runtime command-ID deduplication remains required.

A new holder requeues only its Session's in-flight materialization and commands. It preserves the
committed `delivery_attempted` fact and command IDs. Pending-work scans include in-flight work
with no runnable queued head. The head rule continues to serialize commands within a Session.
Acquiring a lease requires only the Session ID, including when no Environment exists.

Worker writes take the shared side of the existing Sandbox deletion advisory lock, then the
Session admission advisory lock, then fence the Session lease. Environment cleanup takes the
exclusive deletion lock while settling Dispatch outcomes and marking Sessions. This
serializes `MarkDeliveryAttempt` and settlement with deletion without holding the lock between
transactions or across an RPC.

Cleanup also locks every affected Session lease before touching delivery rows. It creates an
expired placeholder when a lease row is missing, in sorted Session order, so lease acquisition
serializes with cleanup. The no-op conflict update keeps an existing holder and expiry unchanged
while locking it. Cleanup invalidates those leases in its transaction. A marker committed first
remains possible forwarding evidence; cleanup committed first prevents a marker or settlement
from reviving work.

## Delivery scheduling and recovery

Each replica feeds a rate-limited workqueue keyed by Session ID. A committed command or Session
change sends its bare Session ID on the delivery `NOTIFY` channel. The listener hands every payload
directly to the deduplicating queue; bounded channels never discard distinct Session keys. A head
step that completes requeues its Session for the next head. Transient claim errors in command
and Session materialization schedule that Session's exponential backoff, from 500 ms up to 30 s.
Other transient processing and delivery errors use the same keyed retry path. Wake retry times
and readiness deadlines retain their existing per-Session delayed scheduling.

A replica subscribes before its startup resync, which lists current Sandboxes, confirms deletion
candidates with the API server, and seeds all Sessions with pending or in-flight work, including
work without a lease row. `Ready` closes after the first successful complete resync. Listener loss
invalidates `wakeups.Hub.Generation()`; after LISTEN is re-established, the generation change
requests another full resync. Commits in the connection gap are recovered from PostgreSQL.
Notifications delivered while listening are lossless in-process. There is no periodic delivery
rescan and no recovery promise for a deliberately suppressed notification on a healthy connection.

If another replica holds the Session lease, the worker schedules that exact Session at the reported
lease expiry. It rechecks the lease then, rescheduling if renewed, or recovering only that Session's
in-flight work under a fresh holder. A crashed holder therefore needs no inventory sweep: other
replicas already saw the same committed work notification, or seed it on startup/reconnect.

Sandbox informer adds, updates and deletes enqueue the exact Sandbox UID. Relisting after a watch
gap retains deletion detection. Reconciliation reads only that UID's Sessions and enqueues them;
API-confirmed absence runs `EndSandbox` for that UID. Cached absence and a same-name replacement
cannot authorize delivery or bypass the deletion fence. Database and API failures retry the affected
Sandbox key.

During an overlapping deployment, empty delivery notifications from pre-keyed replicas request a
full resync. Both implementations use the same Session leases and Runtime command-ID deduplication;
neither can concurrently commit a head step under another holder. Keyed notifications remain ordinary
invalidations to old replicas. This scheduling change needs no schema migration; rollback reverts it.

## Session watch

`WatchSessions` and its generated `GET /v1/sessions:watch` JSON facade use exactly
`ListSessions`' retained-owner read scope, including waiting and failed materialization without
History or a live Environment. Configured component services read all Sessions; Identity callers
read only their exact retained execution Identity. Typed delegation remains method-scoped.

An empty `resume_token` streams current Session records in version order, then `CaughtUp`.
Every frame includes an opaque, versioned resume token containing the last version and a digest
of the resource and effective read filter. A nonempty token resumes current records with
`version > last`; intermediate writes to one Session can coalesce. Tokens carry no authority.
Malformed, unsupported, future-version or different-filter tokens return `INVALID_ARGUMENT`.
The read filter is the authenticated caller's retained owner scope, not a client-supplied Identity.
There is no token expiry or retention floor: Session rows are never deleted, and every changed
record remains available at its latest version. This is a state watch, not an audit log.

Every Session insert/update advances a transactional database counter, including old binaries'
writes during a roll. The counter lock serializes version allocation through commit, so a reader
cannot advance past a smaller uncommitted version. The additive migration backfills retained rows.
Notifications contain only routing keys for all-Sessions and exact-Identity subscribers; streams
re-read committed rows through `postgres/wakeups.Hub` and keep no per-Session deduplication map.
Reads are bounded batches. Listener loss/reconnect changes its generation; active watches replay
a full current snapshot followed by `CaughtUp`, even if unchanged rows precede their prior token.
Clients upsert by Session ID and retain the newest frame's token, including after that replay.
A disconnected listener is not marked caught up. Streams end at the configured Dispatcher
watch lease for renewed authorization (`reconnect_required` when idle); slow writes have the same
bound as Dispatch watches.

`SessionStatus.activity` is shared by Get, List and Watch. It records a timestamp and either
an observed HarnessHost Session verbatim, Unknown (`NOT_YET_OBSERVED` or `STREAM_LOST`), or
EnvironmentDown (`SUSPENDED` or `DELETED`). A missing record reads as Unknown/NOT_YET_OBSERVED,
with the Session creation time as its timestamp. Hosted Sessions are observed through one HarnessHost watch per Sandbox, using the Environments
proxy under the Sessions SVID. Central Sessions use one watch of the configured sampler under the same SVID. Without a configured sampler they remain Unknown.
Stored records decode as protobuf with unknown fields preserved. Undecodable records and
unrecognized future activity variants read as Unknown/STREAM_LOST, never idle.
Activity updates use the same transactional Session versions and authorized publication path.

## HarnessHost activity observation

Each Dispatcher replica runs a HostActivityObserver independently of delivery workers. Sandbox
informer events and committed Session versions identify exact Sandbox UIDs with hosted Sessions.
Listener reconnect rescans committed rows. A PostgreSQL `sandbox_activity_watch` lease grants one
replica authoritative observation per Sandbox; contenders retry at its database-reported expiry.
Replica loss permits takeover after expiry. Lease renewal failure cancels the stream.

The observer requires exactly one `x-agentplane-harness-host-session-version: 1` response header
before decoding the first frame. Older Runtimes, missing/duplicate headers and unsupported versions
read Unknown; they cannot be interpreted as an idle Session. Connections use the same exact Sandbox
routing and Sessions service SVID as native command delivery. No Sandbox restart is automatic.

Every connection starts a fresh snapshot. Upserts are buffered through each CaughtUp before being
stored verbatim as Observed. Missing Sessions remain Unknown. Stream loss publishes STREAM_LOST;
reconnect uses bounded backoff and a fresh snapshot. There is no host polling or resume token.
The forwarded stream ends when Environments' Runtime connection ends, fencing its incarnation.
Each stream additionally gets a UUID epoch: activity transactions lock and check both the live
PostgreSQL lease and its current epoch, so late callbacks cannot overwrite a newer stream.
Reads project Observed or SUSPENDED without a matching live lease/epoch as Unknown/STREAM_LOST, even
before takeover publishes the invalidation. Every stored activity update uses the existing
transactional Session versions and authorized WatchSessions publication.

EnvironmentDown is written only after an uncached exact-UID API check confirms suspension
(`OperatingModeSuspended` and `StopStageStopped`) or deletion; the lease-fenced transaction repeats
that check. Unready, unreachable and absent cached objects never prove a stopped Environment.
Sandbox observations do not write activity for central Sessions, even when they share an executor Sandbox.

A suspended Environment keeps the observer lease while the informer watches for lifecycle changes.
Lease renewal revalidates suspension against the uncached API; uncertainty ends the observation.
DELETED is permanent for the exact UID and survives observer loss.

Central observation reuses the same stream processing, wire-version guard, lease renewal and epoch
fencing. The existing `central_sampler` target supplies its connection and Sessions SVID. Its
singleton `central_sampler=true` row in `sandbox_activity_watch` leases one sampler watch across all
Dispatcher replicas, independently of every executor Sandbox. Hosted rows
retain their unique Sandbox UID; exactly one key kind is present per row. A lost replica is replaced
after lease expiry, with a new epoch and full snapshot. The sampler admits the Sessions SVID and
filters snapshots to the service owner's Sessions. Only locally recorded central Sessions are
updated, and a central observation must carry the `central_sampler_v1` host variant. Central
Sessions never read EnvironmentDown, including when their executor is suspended or deleted. A lost
stream, incompatible wire shape, expired lease or stale epoch reads Unknown/STREAM_LOST. NULL remains
Unknown/NOT_YET_OBSERVED. Get, List and Watch publish the host's Session verbatim, preserving fields
from newer writers.

### Operator acknowledgement of delivery notices

Dispatcher records shared operator attention in a separate table. Its identity is the exact Session,
command ID and positive receipt version. The App's authenticated operator proxy calls
`AcknowledgeCommandNotice` with its service identity. Owning a Session allows reading its
acknowledgements, but only the App service can mark a notice seen. Every lookup and history page
rechecks the retained Session's read authority. Closing or deleting the Environment leaves these
operations available to the operator.

A first acknowledgement must match a current FAILED or UNKNOWN receipt, including the latest
visible legacy deletion receipt. Dispatcher checks the original row and stores the reference and
acknowledgement time. A duplicate preserves that time even after its receipt changes or expires.
A newer revision can require attention independently. Acknowledgement changes neither receipt
version nor outcome and supplies no execution evidence. Receipt storage and recovery remain
independent of operator attention.

Through the existing Dispatcher proxy, POST to `/v1/sessions/{session_id}/notice-acknowledgements`
records attention, POST to its `:batchGet` method checks 1–100 exact revisions, and GET pages the
acknowledged references newest first (default 25, maximum 100). The history includes superseded
or expired references. Only Dispatcher receipt reads can supply original details when retained.
Receipt enumeration covers live rows; the recent workset has bounded legacy coverage, and a known
legacy ID can be checked through WatchDispatches while its tombstone remains retained. Older
replicas return UNIMPLEMENTED for the new methods; callers keep unconfirmed notices visible.
