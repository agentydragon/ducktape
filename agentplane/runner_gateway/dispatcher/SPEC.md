# Session Dispatcher caller authority

This contract governs SessionDispatcher gRPC methods served by the Runner Gateway and the generated
HTTP/JSON facade served by the App.
It scopes existing Sessions and existing environments; it does not create environments or select templates.

## Caller proof

- One TLS gRPC listener serves Dispatcher, EnvironmentService, Gateway proxy, Executor and health
  methods. Its bearer allowlist declares exactly the Dispatcher and Gateway audiences. Dispatcher
  and EnvironmentService methods use the Dispatcher audience and their explicit caller and
  backend-account allowlists; Gateway proxy and Executor methods use the Gateway audience and their
  separate allowlists. Dispatcher and EnvironmentService methods also admit verified Sandbox SVIDs.
  Delegation remains scoped to its exact configured methods and rechecks the effective caller's
  current ServiceAccount UID. The resolver selects by presented credentials before method admission,
  rejects disabled or ambiguous credentials, and never falls back from an invalid presented SVID.
- Sandbox traffic reaches the shared TLS gRPC listener through central-proxy CONNECT, originating
  TLS with its Runner SVID. It does not use an HTTP/JSON facade. No Dispatcher-audience token is
  projected into Sandbox Pods.
- A normal caller is the ServiceAccount identity returned by TokenReview, including the actual ServiceAccount
  UID. Request fields never assert this identity.
- The existing configured backend account list remains a privileged compatibility path only when the same
  TokenReview also supplies a complete Pod name and Pod UID. An account-only token with a configured backend
  name is still a normal account caller. The generic `auth.Principal` and `auth.Caller` APIs continue to mean
  Pod-bound identity.
- Creator/broker identity is retained for provenance only. It is never Session authority.

## Delegated callers

- An independent `delegation` setting names the Actions ServiceAccount, the namespaces whose caller identity it may assert,
  and the exact full RPC method names that accept delegated calls. It does not inherit `dispatcher_callers` or
  `wake_callers`; the latter remains a separate trusted automatic-wake broker grant.
- Shared workload-auth middleware admits the Actions token only on the configured Dispatcher and EnvironmentService methods and rechecks the effective caller's current ServiceAccount UID
  before the handler runs. Direct and delegated calls both use the typed `EffectiveCaller` admission. Missing or replaced
  ServiceAccounts fail closed, and backend lookup failures return `UNAVAILABLE`.
- Delegated calls always use ordinary Session ownership checks, even when the Actions actor is in `dispatcher_callers` or
  arrives with Pod proof. The effective caller must match the opening's retained execution identity and the current
  controller-owned account for writes. Reads use the same retained owner scope as direct calls.
- Wake eligibility is evaluated against the effective caller. Actor membership in either backend allowlist does not grant
  the effective caller service-wide access, opening-before-ready authority, or wake authority.
- Durable opening and dispatch rows retain the verified actor, effective caller and any verified actor Pod proof separately.
  Action request and execution UUIDs are correlation fields only. Existing Session IDs, command IDs and domain request
  equality remain the reconciliation keys; a retry returns the original attribution and cannot duplicate an effect or
  rewrite its admission evidence.
- No credentials or Action lease token are persisted. The Gateway performs no Actions or app lookup. Delegation does not
  alter the Runner transport boundary below.

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
  after explicit deletion. No permanent Session UUID marker is planned, and retained History can reject conflicting reuse.
- A new normal `CreateSession` can target only the current Sandbox-owned ServiceAccount whose namespace, name and UID
  equal the authenticated caller's reviewed ServiceAccount. The Sandbox and account are read from Kubernetes;
  callers cannot choose a different principal by supplying strings.
- An existing `CreateSession` request is a read-only idempotent retry. Before comparing its immutable request,
  Dispatcher checks the same retained owner UID or configured Pod-bound backend authority as receipt reads;
  an unauthorized caller gets `NOT_FOUND` without conflict details. The comparison is shared with
  transactional creation and includes exact environment name/UID, deterministic spec bytes, and optional
  setup bytes. Matching retries do not consult live environment inventory or change materialization,
  attribution, pending principal binding or wake state. Delegated calls still pass middleware's current
  effective-identity validation and never inherit their actor's privilege. An absent row still requires
  normal fresh admission; this path provides no historical deletion registry.
- Every new opening retains the target execution ServiceAccount as its immutable logical principal and stores
  the authenticated caller separately as creator. A normal caller can read that opening only while its
  authenticated account and UID equal the retained target principal.
- Before the controller has created the ServiceAccount, a configured Pod-bound backend may open against the
  expected `<sandbox namespace>/<sandbox name>` account with a pending UID. Reconciliation binds the first UID
  only after reading the exact Sandbox UID and verifying the ServiceAccount's controlling OwnerReference names
  that Sandbox UID. The bind is conditional and cannot overwrite a UID already recorded.
- Dispatcher is the authority for the Identity a Session executes as. Every new opening also records the
  Identity its Sandbox names in `spec.identity`, read live with its UID, beside (never instead of) the execution
  ServiceAccount; a Sandbox naming an absent Identity cannot open a Session (`FAILED_PRECONDITION`). Dispatcher
  passes both to the runtime or central sampler when it places the Session. Placement and command delivery
  require the Sandbox to still name that exact Identity incarnation, or none for a Session recorded with none;
  otherwise the work fails `execution_identity_changed`, so an Identity changed while the Sandbox was stopped
  strands its Sessions rather than re-attributing them.
  `Session.identity` reads that Identity back (absent when the Sandbox named none), beside
  `Session.execution_identity`, the execution ServiceAccount.
- Legacy openings whose owner columns are NULL stay distinct from new/pending rows. The current owner of a
  same-named ServiceAccount is not retroactively granted those Sessions.

## Method scope

- SubmitInput, ChangeModel, ChangeReasoningEffort and InterruptTurn require an existing Dispatcher Session
  record for every caller, including configured Pod-bound backends. The supplied Sandbox name and UID must
  match that record. Missing records and records hidden from the caller return `NOT_FOUND`, before command
  conflict details. No Session record is reconstructed from Runtime journals or History.
- Normal Session commands and CancelDispatch require the caller to match
  the opening's execution account and Sandbox UID. Normal calls also compare the authenticated UID with the
  current controller-owned account UID. A changed or missing managed account cannot authorize a write.
- GetSession, ListSessions, GetDispatch and ListDispatches filter retained rows by the recorded target account and UID.
  These reads do not require a live Sandbox or ServiceAccount. Old rows without a trustworthy owner are hidden
  from normal account callers but remain visible through existing privileged backend behavior.
- WatchDispatches applies the same filter to durable snapshots and deletion terminal events. The terminal
  event's owner association is carried through bounded PostgreSQL notification parts so a row deleted from
  storage cannot bypass the read filter.
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
- The worker checks the live managed account before binding a pending owner and again
  immediately before Runner delivery. Source errors fail closed; missing pending accounts remain queued, while
  a changed bound account fails queued work instead of delivering it under the replacement identity.
- The delivery router requires the Session record and checks its stored Sandbox name and UID for
  native and central Sessions before selecting the backend. A missing record never falls back to
  a native Runtime. This check is not an atomic fence against deletion after the read; managed
  quiescence and Runtime's admission barrier remain separate.
- Automatic wake is attempted only when the live Sandbox has `spec.wakeOnSend: true`. The shared lifecycle service
  rechecks the target UID, the retained effective caller's current ServiceAccount UID, and current `resumePolicy` before
  requesting resume. The existing `wake_caller_accounts` setting is a separate trusted broker grant for automatic
  wake only; it does not grant explicit resume authority or override `SuspendedBy=Requested`. Dispatcher actors and
  admission-time eligibility are never substitutes for the effective caller's current authority.
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
- Central sampler openings and their inputs bypass executor readiness and automatic wake. The suspended execution
  environment does not block delivery to the central sampler.

## Transport boundary

Sandbox Pods retain proxy-only egress. Their credential-holding relay reaches the shared TLS gRPC
listener through central CONNECT; the listener authenticates the Runner SVID before applying the
unchanged Dispatcher and EnvironmentService method rules. The configured CLI credential string
remains inert for compatibility. Backend callers use the same listener with the configured
Dispatcher audience and allowlists. A concrete MCP/client and History provenance propagation remain
outside this transport change.

## Typed delivery observations

Dispatch and Session-opening receipts carry an optional `delivery_blocker` and a typed
`wake_assessment`, independently of their delivery state. These fields round-trip through durable
storage, Get/List/Watch and `sessions__observe_input`. A changed assessment is a watch update even
when delivery state and reason are unchanged. An absent stored assessment reads as
`WAKE_STATE_NOT_EVALUATED`; this does not claim a wake request was authorized or attempted.

Automatic wake is assessed and requested through the shared lifecycle manager by the Gateway's
in-process DeliveryWorker. The worker preserves the original effective caller and records the
manager's typed assessment; see the lifecycle manager SPEC for its policy and mutation contract.

## Deletion admission fence

Once Dispatcher cleanup for an exact Sandbox UID commits, later `CreateSession` and queued-command
admissions for that UID fail with `FAILED_PRECONDITION`, including privileged callers using an
inventory snapshot read before cleanup. The durable UID tombstone survives removal of dispatch and
ownership rows and process restart. An ownership claim cannot recreate the removed
worker fence. A same-name environment with a different UID has independent admission.

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
all admitting/cleanup Gateway replicas use this protocol; the additive migration alone does not
fence old writers.

## Native opening outcomes and dependent commands

The Dispatcher retains logical existence and the immutable creation request after a native owner
rejects materialization. An exact opening retry reads that receipt; changing configuration requires
a new Session UUID. Runtime admission reserves a Session, not a running native harness.

A native owner `CreateSession` response of `INVALID_ARGUMENT` proves validation rejected the request before
reservation; `ALREADY_EXISTS` reports a conflicting retained request. Dispatcher records these as
`SESSION_MATERIALIZATION_FAILED` and, in the same transaction, fails commands still `QUEUED_IN_DISPATCHER` and never forwarded with
`session_opening_rejected`; a previously attempted command becomes `UNKNOWN`. It does not alter delivering or admitted commands. Admission and
settlement serialize on the Session ID, and settlement checks the current owner generation.
A command admitted first is included; a later new queued command is refused with
`FAILED_PRECONDITION` without a stored receipt. Exact existing-command retries still return the
original receipt after ordinary caller authorization.

`ABORTED`, `UNAVAILABLE` and deadline responses keep the existing same-ID reservation reconciliation
and backoff. Runtime recovers the durable reservation before native preparation; these retries are
not permission to replay uncertain native harness effects. Other native opening errors, including
`INTERNAL`, `UNKNOWN`, `RESOURCE_EXHAUSTED` and unproven preconditions, become `UNKNOWN`. Their
pending commands remain queued with `session_opening_unconfirmed`; they do not wake the environment
or reach Runtime. New queued commands are refused as above. Older `FAILED` openings lack proof of
rejection and receive the same hold without rewriting their stored outcome. Held commands remain
cancellable. Automatic resolution of those held openings is not provided.

These rules apply to native openings after all Dispatcher replicas run this protocol. Central
sampler admission retains its existing behavior; new commands require a Dispatcher Session record.
Deletion cleanup, retained History and UUID reuse policy are unchanged.

## Possible command forwarding and deletion outcomes

Before a queued command RPC leaves Dispatcher, its current fenced worker commits a sticky
`delivery_attempted` fact under the same ownership lock used by environment cleanup. A removed row
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

Deletion snapshots are transactional PostgreSQL notifications to connected `WatchDispatches`
listeners. Cleanup then removes the command receipts but keeps the Session record. These notifications provide no
durable outcome read for a later subscriber. An already-terminal row is not included in cleanup
snapshots: a connected watcher delayed behind its earlier invalidation can miss that outcome when
cleanup removes the row. Broadcasting all retained receipts is not a bounded solution: terminal
subscription buffers can overflow, and reconnect has no retained receipt to recover. Runtime journals and History remain separate evidence
owners; cleanup neither deletes retained History nor converts missing evidence into failure.

Treating an unmarked claim as known-unsent requires a cutover with no old command writers still
running; overlapping old writers do not provide this evidence. A lost marker-commit reply is retried
under ownership before any RPC. If ownership or the request is lost, no RPC is issued; a committed
marker without a send remains conservatively uncertain.
The migration adds the column without altering authored data or History. A rollback to old writers
loses the forwarding-evidence guarantee even if the column remains present.

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

**Gotcha:** the current migration baselines assume Dispatcher migration 11, central sampler
migration 10 and History migration 8 have run; a database restored from before them must run an
image that still carries those migrations first.
