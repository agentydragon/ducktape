# Action Service

The service owns durable ActionRequests, Decisions, and single-shot Executions. Authenticated
callers see only their own requests; the separate operator interface can review and decide them.
Caller-provided provenance never assigns authority. Retry with the same caller-scoped idempotency
key recovers the same request, not another execution.

## External Connection authority

An external caller is a Kubernetes ServiceAccount carrying the label
`agentplane.allegedly.works/action-caller: "true"` in a namespace the service accepts callers
from; the service watches those ServiceAccounts and lists the eligible ones to the operator. The
single operator can inspect Connections, rename them, and unbind them. A Connection's UUID does
not change on rename; names are presentation and need not be unique.

The trusted OAuth adapter reserves a pending grant only after consent, then activates it after
verified issuance. A pending grant has an activation deadline and cannot authorize calls. Exact
binding retries recover the same grant and Connection; conflicting retries are refused.
Each grant retains the ServiceAccount it acts as, its exact verified downstream issuer/client
pair, Connection UUID and binding revision. Those fields do not change on rename or reconnect.

Reconnect revokes the previous grant and creates a new pending revision. Existing credentials never
acquire the replacement ServiceAccount's authority. Unbind and grant revocation take effect on the
next authority resolution; a ServiceAccount that is missing, unlabeled, or not yet seen by the
watch also refuses resolution. Revoking an old grant cannot revoke its replacement. No grant or
Connection history is deleted. A grant bound to a configured Identity, the pre-ServiceAccount
authority, stays readable in the inventory and on its Actions' provenance and never resolves
again; fresh OAuth selecting a ServiceAccount is how such a Connection regains authority.

Resolved external principals use the ServiceAccount as their receipt/idempotency scope:
Connections acting as the same ServiceAccount share reads, while different ServiceAccounts remain
isolated. The submitting grant is separate provenance. The Connection authority is implemented
independently of the OAuth adapter; no external bearer admission is enabled by these management
endpoints.

Each externally admitted Action retains an immutable snapshot of its submitting grant independently
of caller-supplied metadata. Shared-ServiceAccount duplicate submissions return the first Action and
its original provenance; rename, reconnect, refresh and revocation cannot rewrite that evidence.
Admission and dispatch authorization are atomic with revocation. Dispatch requires the original
grant and its ServiceAccount still to authorize the Action, not a later replacement binding.
Loss of authority before the dispatch claim fails the unstarted Execution without changing its
historical Decision or invoking an executor. Revocation does not stop already claimed work.

## Action policies

An `ActionPolicySet` holds typed policies in `autoApproveIf`, `autoDenyIf` and `autoDenyUnless`;
an `ActionPolicyBinding` joins one subject, a labeled ServiceAccount or a live Sandbox by name and
UID, to sets by name, optionally until `expiresAt`. Both are namespaced Kubernetes objects the
service watches in the namespaces it accepts callers from. It reads `spec` only and reports in
each object's `Ready` condition, stamped with the generation it judged, whether the spec parsed;
an invalid set or binding contributes nothing.

Two policy kinds exist. `exact_actions` matches a listed Action by name alone; `argument_schema`
also requires the arguments to satisfy a JSON Schema with plain JSON Schema semantics, so
`properties` alone never implies presence. This version decides from `autoApproveIf` only; the
deny lists are accepted and validated and produce no Decision.

Policies are evaluated once, at admission, against the objects as the service holds them then:
the caller's unexpired bindings, whose subject is matched from the authenticated Sandbox's
namespace and UID or the Connection's ServiceAccount and never from any request field, the
existing valid sets they name, and the first `autoApproveIf` policy that matches. A match
auto-approves with an Execution; no match leaves the request on the human path; until the watch
has synced, every caller is human-only. A later edit, expiry or deletion changes the next Action's
Decision, not this one's; dispatch re-checks only caller authority. The Decision records the
bindings with their resource versions, the sets with their generations, and the matching policy.

## External OAuth consent

An enrollment names one validated OAuth authorization and expires within fifteen minutes. The
operator previews and decides it through an authenticated, browser-bound interface. Only that
browser and operator can decide or recover its result. Approval selects a labeled caller
ServiceAccount and either a new Connection name or an explicitly confirmed existing Connection and
its reviewed version; denial grants no authority. A changed or stale decision cannot
overwrite the original, and an exact retry recovers it.

Existing-Connection selection supports reconnecting to the same ServiceAccount or changing it
only through fresh OAuth. Version changes after consent refuse replacement. Reserving the new
grant at token exchange revokes prior grants before activation; failed issuance never restores
them. Consent alone does not revoke the existing grant, and old credentials never retarget.

Before token issuance, the authenticated upstream operator must match the consent operator.
Consent does not itself activate a grant. Each approved enrollment permits only one token-family
exchange claim; an ambiguous failure after that claim requires fresh authorization. An old
authorization cannot be matched to replacement consent by reusing its client/redirect/PKCE tuple.

## Cancellation

Only the authenticated owning caller can cancel a request. Cancellation takes no expected version;
operator-all review authority does not grant a cancellation override. Pending Decisions and approved
but unclaimed Executions can be cancelled atomically against approval and dispatch. Success guarantees
the request cannot subsequently execute, including after restart or late approval. The dispatch
claim is the cutoff, even before the executor has physically started; dispatching, running and
execution-unknown requests refuse cancellation. No interruption is propagated to executors.

Already-cancelled requests return idempotent success; denied, succeeded and failed requests return
unchanged finished receipts. Cancellation preserves prior Decisions and records the authenticated
actor and time in the canonical event history. Reusing the original submission idempotency key
recovers the cancelled request; an intentional new attempt requires a new key.

## Bounded receipt waits

A receipt wait can return immediately or wait up to 30 seconds for either a resolved Decision or
an Execution outcome. Allowed but undispatched/running work satisfies the Decision predicate only.
Denied, cancelled, succeeded, failed, and execution-unknown receipts satisfy both predicates.
Execution-unknown is a returnable outcome, not proof of success or permission to replay.

Waits use pushed committed-state invalidations, including updates written by another process.
The durable receipt remains authoritative; duplicate/coalesced notifications do not manufacture
transitions. Setup cannot miss a concurrent commit. A deadline returns the current receipt, while
disconnect or wait cancellation only releases the wait and never cancels or resubmits the Action.
Loss of the notification channel fails waiting explicitly; immediate reads remain available and
there is no periodic state-query fallback.

## Actions MCP frontend

The Action Service serves a generic MCP frontend in the same process, with the same catalog,
admission, Decision, Execution, and receipt authority as its HTTP interface. Its fixed tools
discover Actions, submit/cancel requests, read receipts, and page durable events; individual Actions are
not mirrored into MCP tools. Catalog responses omit input schemas and full descriptions unless
explicitly requested. Lists and wait durations are bounded, and backend configuration is never
exposed.

Every MCP HTTP request authenticates its bearer, with live workload validation for Sandbox callers.
Reused MCP session identifiers confer no authority. Long waits revalidate
caller authorization before returning data. Operator bearers, unexchanged credential placeholders,
and caller-supplied identity/policy claims cannot acquire this authority. An Origin header does not
grant authority or categorically disqualify a caller; FastMCP provides automatic Host/Origin
protection for loopback access. Browser cookies are not authentication on this endpoint.
When external OAuth is configured, the frontend also accepts verified, active Connection grants.
Clients may use DCR registration or HTTPS Client ID Metadata Documents (CIMD).
Neither registration nor metadata discovery is authority. Consent binds one labeled caller
ServiceAccount and named Connection to the validated authorization interaction and approving
operator; upstream issuer/subject must match the explicit operator mapping before token issuance.
One consent permits at most one token family. Ambiguous post-claim issuance requires fresh
authorization.

Refresh and bearer admission resolve current canonical grant validity. Ended bindings cannot
acquire replacement ServiceAccount authority. External receipts/idempotency are
ServiceAccount-scoped while each Action permanently records exact submitting
Connection/grant/revision/issuer/client evidence.
Sandbox authentication remains live-workload-based; neither path accepts operator credentials as
a caller bypass. OAuth protocol state shares durable encrypted storage and stable configured keys
across replacement/replicas. Enabling these contracts does not imply deployed client acceptance.

MCP cancellation uses the same owner-only, pre-dispatch-claim cutoff and typed outcomes as the
HTTP cancellation route. It requires no version and never stops in-progress execution. This is
an explicit tool operation, separate from cancelling or disconnecting a receipt wait.

## Execution ownership and shutdown

MCP execution renews its ownership lease while waiting for schema discovery and the tool result.
Renewal proves that the local owner is alive, not that the backend is making progress. Loss of
ownership, inability to renew, or an execution deadline stops local waiting and reports an unknown
outcome. None is evidence that a remote side effect stopped or permission to replay it.

Termination removes readiness and refuses new traffic before waiting for HTTP shutdown. Queued
work is left unclaimed for another replica. A claim already in progress belongs to the draining
replica, which keeps execution and executor liveness active while waiting for its outcome.
Completed work is persisted before adapters and database connections close. The drain has a bounded
budget; forced cancellation records uncertainty when storage is available, otherwise lease expiry
recovers it. Claimed work is never automatically replayed.

## Optional MCP backends

Backend availability is independent of Action Service readiness and HTTP/OAuth availability.
Malformed local bindings fail startup; unavailable peers, runtime credentials, linkage, and
invalid discovered catalogs affect only their group and recover without a service restart.
Discovery exposes replica-local, credential-safe lifecycle diagnostics, never stale runnable tools.

Approved work remains durably unclaimed during temporary backend outages. Revoked authority
still becomes terminal; removed Actions and incompatible schemas are not treated as outages.
Execution pins one connection generation and never automatically replays an ambiguous call.
A tool's own error output is one of its two valid answers and completes the Action; the caller receives it flagged as an error. Only unreachable backends and unknown outcomes fail an execution. Draining stops new claims and reconnects while keeping
in-flight execution leases and connections through bounded result persistence.
