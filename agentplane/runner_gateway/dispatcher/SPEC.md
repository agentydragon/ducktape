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
  `wake_callers`; those remain separate direct-caller policies.
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

- A normal `OpenSession` can target only the current Sandbox-owned ServiceAccount whose namespace, name and UID
  equal the authenticated caller's reviewed ServiceAccount. The Sandbox and account are read from Kubernetes;
  callers cannot choose a different principal by supplying strings.
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
- GetSessionOpening, GetDispatch and ListDispatches filter retained rows by the recorded target account and UID.
  These reads do not require a live Sandbox or ServiceAccount. Old rows without a trustworthy owner are hidden
  from normal account callers but remain visible through existing privileged backend behavior.
- WatchDispatches applies the same filter to durable snapshots and deletion terminal events. The terminal
  event's owner association is carried through bounded PostgreSQL notification parts so a row deleted from
  storage cannot bypass the read filter.
- Store-level writes recheck caller, Session and target Sandbox binding in the same PostgreSQL transaction that
  appends or cancels work. The worker checks the live managed account before binding a pending owner and again
  immediately before Runner delivery. Source errors fail closed; missing pending accounts remain queued, while
  a changed bound account fails queued work instead of delivering it under the replacement identity.
- Existing wake eligibility remains governed by `wake_caller_accounts` and `Sandbox.spec.wakeOnSend`. A
  normal target account is not implicitly added to `wake_caller_accounts`; its work remains queued while the
  Sandbox is suspended unless an existing authorized caller wakes it.

## Transport boundary

This change does not add Runner-to-Dispatcher network reachability or project a Dispatcher token into a Runner
Pod. Managed Runner Pods retain their existing HTTPS proxy-only egress. A follow-on transport change must use the
sidecar-only projected-token and inert-placeholder pattern used by the Actions CLI: add the configured
Dispatcher audience to `cluster/cdk8s/agentplane/sandbox_pod.py`, add an exact Dispatcher destination and
credential rule to `cluster/cdk8s/agentplane/egress.py`, and connect a concrete MCP/client to that proxy route.
The actual MCP/client and History provenance propagation are outside this foundation. Until they are implemented,
this change does not claim a complete end-to-end spawning loop.
