# Action Service

The service owns durable ActionRequests, Decisions, and single-shot Executions. Authenticated
callers see only their own requests; the separate operator interface can review and decide them.
Caller-provided provenance never assigns authority. Retry with the same caller-scoped idempotency
key recovers the same request, not another execution.

## External Connection authority

Configured Identities are reusable authority names, independent of runtime Connections and Threads.
The single operator can inspect Connections, rename them, and unbind them. A Connection's UUID
does not change on rename; names are presentation and need not be unique.

The trusted OAuth adapter reserves a pending grant only after consent, then activates it after
verified issuance. A pending grant has an activation deadline and cannot authorize calls. Exact
binding retries recover the same grant and Connection; conflicting retries are refused.
Each grant retains its configured Identity, exact verified downstream issuer/client pair,
Connection UUID and binding revision. Those fields do not change on rename or reconnect.

Reconnect revokes the previous grant and creates a new pending revision. Existing credentials never
acquire the replacement Identity's authority. Unbind and grant revocation take effect on the next
authority resolution; a missing or disabled configured Identity also refuses resolution. Revoking
an old grant cannot revoke its replacement. No grant or Connection history is deleted.

Resolved external principals use configured Identity as their receipt/idempotency scope: Connections
bound to the same Identity share reads, while different Identities remain isolated. The submitting
grant is separate provenance. The Connection authority is implemented independently of the OAuth
adapter; no external bearer admission is enabled by these management endpoints.

Each externally admitted Action retains an immutable snapshot of its submitting grant independently
of caller-supplied metadata. Shared-Identity duplicate submissions return the first Action and its
original provenance; rename, reconnect, refresh and revocation cannot rewrite that evidence.
External Actions initially require human approval, independent of existing workload auto-decisions.
Admission and dispatch authorization are atomic with revocation. Dispatch requires the original
grant and configured Identity still to authorize the Action, not a later replacement binding.
Loss of authority before the dispatch claim fails the unstarted Execution without changing its
historical Decision or invoking an executor. Revocation does not stop already claimed work.

## External OAuth consent

An enrollment names one validated OAuth authorization and expires within fifteen minutes. The
operator previews and decides it through an authenticated, browser-bound interface. Only that
browser and operator can decide or recover its result. Approval selects a configured enabled
Identity and either a new Connection name or an explicitly confirmed existing Connection and its
reviewed version; denial grants no authority. A changed or stale decision cannot
overwrite the original, and an exact retry recovers it.

Existing-Connection selection supports reconnecting to the same Identity or changing Identity
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
DCR registration alone is not authority. Consent binds one configured Identity and named Connection
to the validated authorization interaction and approving operator; upstream issuer/subject must
match the explicit operator mapping before token issuance. One consent permits at most one token
family. Ambiguous post-claim issuance requires fresh authorization.

Refresh and bearer admission resolve current canonical grant validity. Ended bindings cannot
acquire replacement Identity authority. External receipts/idempotency are Identity-scoped while
each Action permanently records exact submitting Connection/grant/revision/issuer/client evidence.
Sandbox authentication remains live-workload-based; neither path accepts operator credentials as
a caller bypass. OAuth protocol state shares durable encrypted storage and stable configured keys
across replacement/replicas. Enabling these contracts does not imply deployed client acceptance.

MCP cancellation uses the same owner-only, pre-dispatch-claim cutoff and typed outcomes as the
HTTP cancellation route. It requires no version and never stops in-progress execution. This is
an explicit tool operation, separate from cancelling or disconnecting a receipt wait.
