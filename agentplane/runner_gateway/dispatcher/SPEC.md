# Session Dispatcher caller authority

This contract governs the gRPC and generated HTTP/JSON SessionDispatcher methods served by the Runner Gateway.
It scopes existing Sessions and existing environments; it does not create environments or select templates.

## Caller proof

- The Dispatcher TokenReview audience remains the configured Dispatcher audience. Its resolver serves the
  Runner Gateway namespace and namespaces containing explicitly configured Dispatcher backend accounts.
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
- Shared workload-auth middleware admits the Actions token and rechecks the effective caller's current ServiceAccount UID
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

- `OpenSession` requires `session_id` to be a canonical lowercase UUID before reading Sandbox state or recording an
  opening. Read RPCs remain unchanged and can still address retained rows by their stored IDs.
- A new normal `OpenSession` can target only the current Sandbox-owned ServiceAccount whose namespace, name and UID
  equal the authenticated caller's reviewed ServiceAccount. The Sandbox and account are read from Kubernetes;
  callers cannot choose a different principal by supplying strings.
- An existing `OpenSession` request is a read-only receipt retry. Before comparing its immutable request,
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
- Legacy openings whose owner columns are NULL stay distinct from new/pending rows. The current owner of a
  same-named ServiceAccount is not retroactively granted those Sessions.

## Method scope

- SubmitInput, ChangeModel, ChangeReasoningEffort, CancelDispatch and InterruptTurn require the caller to match
  the opening's execution account and Sandbox UID. Normal calls also compare the authenticated UID with the
  current controller-owned account UID. A changed or missing managed account cannot authorize a write.
- GetSessionOpening, ListSessionOpenings, GetDispatch and ListDispatches filter retained rows by the recorded target account and UID.
  These reads do not require a live Sandbox or ServiceAccount. Old rows without a trustworthy owner are hidden
  from normal account callers but remain visible through existing privileged backend behavior.
- WatchDispatches applies the same filter to durable snapshots and deletion terminal events. The terminal
  event's owner association is carried through bounded PostgreSQL notification parts so a row deleted from
  storage cannot bypass the read filter.
- Store-level writes recheck caller, Session and target Sandbox binding in the same PostgreSQL transaction that
  appends or cancels work. The worker checks the live managed account before binding a pending owner and again
  immediately before Runner delivery. Source errors fail closed; missing pending accounts remain queued, while
  a changed bound account fails queued work instead of delivering it under the replacement identity.
- Automatic wake is attempted only when the live Sandbox has `spec.wakeOnSend: true`. The shared lifecycle service
  rechecks the target UID, the retained effective caller's current ServiceAccount UID, and current `resumePolicy` before
  requesting resume. The existing `wake_caller_accounts` setting is a separate trusted broker grant for automatic
  wake only; it does not grant explicit resume authority or override `SuspendedBy=Requested`. Dispatcher actors and
  admission-time eligibility are never substitutes for the effective caller's current authority.
- `Dispatch` and `SessionOpening` keep delivery state separate from wake state. `delivery_blocker` and the typed
  `wake_assessment` are persisted with the receipt and published transactionally to active watches, including updates
  that leave delivery state and reason unchanged. Disabled, denied, rate-limited, failed and uncertain wake outcomes
  remain queued; only the existing post-request readiness deadline can terminally fail work. The assessment records
  its observation time, observed generation, optional retry time and structured status details. The shared lifecycle
  budget charges caller namespace/name/UID plus Sandbox UID and generation. An uncertain patch result is not retried
  during the same observed generation; a later generation triggers a fresh assessment.
- Central sampler openings and their inputs bypass executor readiness and automatic wake. The suspended execution
  environment does not block delivery to the central sampler.

## Transport boundary

This change does not add Runner-to-Dispatcher network reachability or project a Dispatcher token into a Runner
Pod. Managed Runner Pods retain their existing HTTPS proxy-only egress. A follow-on transport change must use the
sidecar-only projected-token and inert-placeholder pattern used by the Actions CLI: add the configured
Dispatcher audience to `cluster/cdk8s/agentplane/sandbox_pod.py`, add an exact Dispatcher destination and
credential rule to `cluster/cdk8s/agentplane/egress.py`, and connect a concrete MCP/client to that proxy route.
The actual MCP/client and History provenance propagation are outside this foundation. Until they are implemented,
this change does not claim a complete end-to-end spawning loop.

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

Once Dispatcher cleanup for an exact Sandbox UID commits, later `OpenSession` and queued-command
admissions for that UID fail with `FAILED_PRECONDITION`, including privileged callers using an
inventory snapshot read before cleanup. The durable UID tombstone survives removal of the opening,
dispatch and ownership rows and process restart. An ownership claim cannot recreate the removed
worker fence. A same-name environment with a different UID has independent admission.

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

A native `OpenSession` response of `INVALID_ARGUMENT` proves validation rejected the request before
reservation; `ALREADY_EXISTS` reports a conflicting retained request. Dispatcher records these as
`FAILED` and, in the same transaction, fails commands still `QUEUED_IN_DISPATCHER` with
`session_opening_rejected`. It does not alter delivering or admitted commands. Admission and
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
sampler admission and commands without a Dispatcher opening retain their existing behavior.
Deletion cleanup, retained History and UUID reuse policy are unchanged.
