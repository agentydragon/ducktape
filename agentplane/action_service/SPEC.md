# Action Service

The service owns durable ActionRequests, Decisions, and single-shot Executions. Authenticated
callers see only their own requests; the separate operator interface can review and decide them.
Caller-provided provenance never assigns authority. A caller-scoped idempotency key is accepted
once: a repeated key is refused, and the request it named is recovered by looking it up by key,
never by another submission.

The service publishes an agent overview at `/llms.txt`, linked Markdown workflow guides under
`/docs/agents/`, and request/response schemas at `/openapi.json`. These static documentation
surfaces do not require caller admission.

## Built-in input contracts

Sandbox and orchestration Action inputs have one Go contract per wire shape. The same typed
contract produces the advertised JSON Schema and validates arguments before decoding them for its
executor. Objects reject unknown properties, including nested objects. Optional nullable fields
accept absence and explicit null; required fields remain required. Integer fields accept integral
JSON spellings such as `60`, `60.0`, and `6e1`, and reject fractions, booleans, null, and overflow.
Invalid arguments expose no validator detail that could quote their values.

Sandbox exec admission advertises the deployment's current timeout and output maxima. Execution
accepts the stable hard ceilings (3600 seconds and 1,000,000 bytes), then clips to the current
deployment maxima. This lets an already-admitted request execute across a settings change that
lowers those maxima. Both profiles derive from the same typed input contract.

Go and Python Sandbox Action names are generated from a shared JSON Schema enum. Externally
supplied MCP schemas and policy-authored argument schemas retain their own JSON Schema contracts.

## Caller admission and Connection authority

Every caller is a Kubernetes ServiceAccount carrying the label
`agentplane.allegedly.works/use-action-service: "true"` in a configured caller namespace.
A Kubernetes token proves that account directly through TokenReview for the configured audience.
The resolver requires an allowed namespace and ServiceAccount UID; Actions accepts tokens with or without a Pod binding. A supplied
Pod binding must have exactly one name and UID. An external MCP bearer proves an active
Connection grant whose currently bound account is admitted by the same label. Nothing is admitted
before the ServiceAccount watch has synced. Missing or unlabeled accounts cannot submit Actions.

The operator can inspect Connections, rename them, rebind them to an eligible ServiceAccount, and
unbind them. Updates carry the reviewed version; an already-unbound Connection returns unchanged.
A Connection's UUID survives rename and rebind; names need not be unique. Each grant retains its original caller and immutable grant
revision, while future requests from its existing token act as the Connection's current bound
account. Rebinding does not require issuing another token.

A grant is created active in the transaction that issues its OAuth tokens. Reconnect revokes the
previous grants and creates a new revision. Unbind, revocation, or loss of the current account's
admission refuses subsequent MCP authority resolution. Revoking an old grant cannot revoke its
replacement, and no Connection or grant history is deleted.

The core receives a ServiceAccount principal and has no Connection authority or grant provenance.
Requests and idempotency keys are scoped to the submitting account. Connections acting as the
same account share its receipt reads; different accounts remain isolated. Rebinding changes only
future submissions, not the account of an existing Action. Revoking a Connection stops further
requests; it does not cancel an Action the core has already accepted.

## Action policies

A `PolicySet` holds typed policies as `action_auto_approve` rules; a `PolicyBinding` joins one
subject -- a namespaced ServiceAccount, which is what a workload's Pod runs as and what an external
Connection acts as -- to sets by name, optionally until `expiresAt`. Both are namespaced Kubernetes
objects the service watches in its configured policy namespace. Labeled caller ServiceAccounts are
watched independently in configured caller namespaces, so a binding can target an account outside
the policy namespace. The service reads `spec` only. A set with a rule that does not parse
contributes nothing; a set a binding names that is missing or refused does not stop its other sets
from granting. The service reports which in the binding's `ActionsReady` condition, beside the other
readers' conditions, and never judges a binding's `expiresAt`: the Policy Controller deletes an
expired binding.

Five policy kinds exist. `exact_actions` matches a listed Action by name alone; `argument_schema`
also requires the arguments to satisfy a JSON Schema with plain JSON Schema semantics, so
`properties` alone never implies presence. `github_repository` requires a listed GitHub MCP call to
target one configured `owner`/`repository`: string `owner`/`repo` arguments compared
case-insensitively, a pull-request search with no query-level `repo:` qualifier, a code search
with exactly one unquoted `repo:owner/repo` qualifier. `github_public_repository` derives the
target the same way and requires a live unauthenticated GitHub lookup to confirm it public; a
lookup that fails or is unavailable matches nothing, so the request stays on the human path and is
never denied for it. `home_assistant_entity_control` confines Home Assistant's one generic
service-call tool, `ha_call_service`, to configured entities and the services configured for each:
the call must name exactly one configured entity, that entity's own domain and one of its
services, and carry no argument the kind has not reviewed and no `data` that retargets it;
anything else matches nothing and stays on the human path. This version decides from
`action_auto_approve` rules only; there is no deny form.

Policies are evaluated once, at admission, against the objects as the service holds them then:
the caller's bindings, whose subject is matched from the ServiceAccount the bearer
proved -- the one its Pod runs as, or the one its Connection acts as -- and never from any request
field, the
existing valid sets they name, and the first `action_auto_approve` policy that matches. A match
auto-approves with an Execution; no match leaves the request on the human path; until the watch
has synced, every caller is human-only. A later edit, expiry or deletion changes the next Action's
Decision, not this one's; dispatch re-checks only caller authority. The Decision records the
bindings with their resource versions, the sets with their generations, and the matching policy.
The request is persisted only once evaluation has finished, together with any auto-decision, so a
request is observable as pending only while it waits for a human.

A caller can read an effective policy: the bindings admission would resolve now for its own
subject, or for a ServiceAccount it names, the sets that resolved, and the auto-approve entries
in evaluation order, each entry named as a Decision's evidence names the matching policy. The read
and the Decision come from one resolution, so they cannot disagree; the answer says whose it is; a
subject the service does not watch reads as no bindings; and it carries `synced`, which is false
until the watch has synced -- then nothing auto-decides
and every request takes the human path, whatever the objects say. A set that is missing or
refused contributes nothing and is simply absent from the caller's view; verdicts, validation
reports and labels are shown only to the operator, who can read the same resolution for any
named subject.

## External OAuth consent

An enrollment names one validated OAuth authorization and expires within fifteen minutes. Its
first authenticated preview pins it to that operator's issuer and subject. That operator can
approve an eligible ServiceAccount and either a new Connection name or an explicitly confirmed
existing Connection and its reviewed version. Denial grants no authority. A different operator
cannot inspect or decide the enrollment; browser identity does not supply authority.

A decision is accepted once. Any later decision, including an identical retry after a lost
answer, is refused and requires restarting authorization. An enrollment version or existing
Connection version that changed since review also refuses the decision or activation.

The upstream authenticated operator must match the consent operator before token issuance. Consent
alone does not activate a grant. An approved enrollment permits one token-family exchange claim;
an ambiguous failure after that claim requires fresh authorization. Activation creates the grant
active in the same transaction as token issuance and revokes replaced grants. An old authorization
cannot acquire replacement consent by reusing its client, redirect and PKCE tuple.

## Cancellation

Only the authenticated owning caller can cancel a request. Cancellation takes no expected version;
operator-all review authority does not grant a cancellation override. Pending Decisions and approved
but unclaimed Executions can be cancelled atomically against approval and dispatch. Success guarantees
the request cannot subsequently execute, including after restart or late approval. The dispatch
claim is the cutoff, even before the executor has physically started; dispatching, running and
execution-unknown requests refuse cancellation. No interruption is propagated to executors.

Already-cancelled requests return idempotent success; denied, succeeded and failed requests return
unchanged finished receipts. Cancellation preserves prior Decisions and records the authenticated
actor and time in the canonical event history. The cancelled request stays readable by its
submission idempotency key; an intentional new attempt requires a new key.

## Receipt reads and waits

Browser Web Push signals changes to pending requests. The service worker reads current receipts before choosing
notification content or decision buttons, and owns their presentation.

`ActionService.GetActionRequest` reads the current receipt once. `WatchActionRequest` streams that
receipt immediately and then on committed changes until `WatchUntil` holds or the client deadline
ends; the last received receipt is current state. The HTTP/JSON facade exposes the generated stream
route described by `/openapi.json`. The receipt read has no `wait_seconds` or `wait_until` query
parameters.

MCP tools retain their bounded `wait.wait_seconds` and `wait.wait_until` options. A receipt wait can
return immediately or wait up to the MCP edge's configured `max_wait_seconds` (30 seconds by default)
for either a resolved Decision or an Execution outcome. Direct MCP tools initially wait for the edge's
`direct_wait_seconds` (30 seconds by default), bounded by that same maximum, before returning a request
ID for a still-running Action.

Allowed but undispatched/running work satisfies the Decision predicate only. Denied, cancelled,
succeeded, failed, and execution-unknown receipts satisfy both predicates. Execution-unknown is a
returnable outcome, not proof of success or permission to replay.

Waits use pushed committed-state invalidations, including updates written by another process. The
durable receipt remains authoritative; duplicate/coalesced notifications do not manufacture transitions.
Setup cannot miss a concurrent commit. A deadline returns the current receipt, while disconnect or
wait cancellation only releases the wait and never cancels or resubmits the Action. Loss of the
notification channel fails waiting explicitly; immediate reads remain available and there is no
periodic state-query fallback.

## MCP edge request path

The standalone MCP edge serves the external `/mcp` and OAuth protocol endpoints and owns
ConnectionService. It resolves an external bearer to the Connection's currently bound
ServiceAccount, then calls Action Service with that workload identity. Action Service does not
serve the external MCP or OAuth endpoints.

The edge exposes a generic MCP frontend backed by Action Service, with the same catalog,
admission, Decision, Execution, and receipt authority as its HTTP interface. Its fixed tools
discover Actions, submit/cancel requests, read receipts and results, and page durable events. A result
reads as the tool that ran answered, an MCP backend's own result exactly with every content block, and a
request without one says what it is waiting on or why it has none. A submission answers the same way once
its bounded wait ends, unless its caller asks for the receipt. A receipt reports an execution's state,
error and timing but never its result, which is only ever answered as the tool answered it. Catalog
responses omit input schemas and full descriptions unless explicitly requested. Lists and wait
durations are bounded, and backend configuration is never exposed.

A caller on an external Connection also sees direct tools: the Actions its group's reviewed
configuration names, each as an MCP tool of its own, listed where a policy bound to the caller could
auto-approve it. A direct call is an ordinary Action with the same caller scope, admission, Decision and Execution. One that no policy decides is refused before anything is persisted, with the reason, so
it never waits for a human; one still running after a bounded wait answers with its request instead
of its result.

Every MCP HTTP request authenticates an access token issued by the edge's OAuth server. Reused MCP
session identifiers confer no authority. The edge resolves each bearer against the current grant
and Connection state; the Action Service separately authenticates the workload identity used for
its core calls. Operator bearers, unexchanged credential placeholders, and caller-supplied
identity/policy claims cannot acquire this authority. An Origin header does not grant authority or
categorically disqualify a caller. Browser cookies are not authentication on this endpoint.
Clients may use DCR registration or HTTPS Client ID Metadata Documents (CIMD).
Neither registration nor metadata discovery is authority. Consent binds one labeled caller
ServiceAccount and named Connection to the validated authorization interaction and approving
operator; upstream issuer/subject must match the explicit operator mapping before token issuance.
One consent permits at most one token family. Ambiguous post-claim issuance requires fresh
authorization.

Refresh and bearer admission resolve current canonical grant validity and the current Connection
binding. External receipts and idempotency are ServiceAccount-scoped. Connections retain their grant
history; accepted Actions retain their submitting account independently of later Connection changes.
In-cluster Actions RPCs and their HTTP facade use live ServiceAccount authentication; workload tokens
do not authenticate to `/mcp`. Neither path accepts operator credentials as a caller bypass. OAuth protocol state is durable across replacement and replicas. Recoverable upstream refresh
tokens are stored in the service database; issued opaque token values are stored only as hashes. Enabling these contracts does not imply deployed client acceptance.

MCP cancellation uses the same owner-only, pre-dispatch-claim cutoff and typed outcomes as the
HTTP cancellation route. It requires no version and never stops in-progress execution. This is
an explicit tool operation, separate from cancelling or disconnecting a receipt wait.

## Execution ownership and shutdown

Execution renews its ownership lease while the attempt is in flight: MCP schema discovery and the
tool result, and a sandbox command for as long as its own timeout allows.
Renewal proves that the local owner is alive, not that the backend is making progress. Loss of
ownership, inability to renew, or an execution deadline stops local waiting and reports an unknown
outcome. None is evidence that a remote side effect stopped or permission to replay it.

Termination removes readiness and refuses new traffic before waiting for HTTP shutdown. Queued
work is left unclaimed for another replica. A claim already in progress belongs to the draining
replica, which keeps execution and executor liveness active while waiting for its outcome.
Completed work is persisted before adapters and database connections close. The drain has a bounded
budget; forced cancellation records uncertainty when storage is available, otherwise lease expiry
recovers it. Claimed work is never automatically replayed.

## Orchestration Actions (v1)

A stopgap that lets agents start and drive other agents now; it is not the intended long-term
design. An `orchestration_v1` group launches Session Sandboxes and opens, drives and reads their
Sessions with this service's own authority, on each request's Decision, and the Session Dispatcher
and the History Service trust it as a caller. A launch writes the grants it names for its caller, the request's authenticated
subject and never an argument: the Notification Service's `send_message` on the new Sandbox's
inboxes, and an `argument_schema` auto-approval of this group's Actions on it. Both are pinned to
the Sandbox's UID and deleted with it. Every Action on a Sandbox names its UID, is refused for any
other incarnation and reads only that incarnation's Sessions. A repeated launch by the same caller with the same arguments adopts its
Sandbox; any other launch of a taken name is refused, and a launch may not bind a PolicySet a
Sandbox owns.

## Optional MCP backends

Backend availability is independent of Action Service readiness and HTTP/OAuth availability.
Malformed local bindings fail startup; unavailable peers, runtime credentials, linkage, and
invalid discovered catalogs affect only their group and recover without a service restart.
Discovery exposes replica-local, credential-safe lifecycle diagnostics, never stale runnable tools.
The same diagnostics are also available to operators, for every mcp-kind group regardless of
whether it uses OAuth linkage or a mounted credential.

Configured remote OAuth linkages identify their client with either a fixed client ID or the Action
Service's one shared public Client ID Metadata Document. CIMD uses that URL as the client ID and
lists the redirect URIs for every linkage that uses it. Linking with the shared CIMD is offered only
when the remote authorization server advertises CIMD support. Both modes retain the configured
redirect URI and PKCE flow.

Approved work remains durably unclaimed during temporary backend outages. Revoked authority
still becomes terminal; removed Actions and incompatible schemas are not treated as outages.
Execution pins one connection generation and never automatically replays an ambiguous call.
A tool's own error output is one of its two valid answers and completes the Action; the caller receives it flagged as an error. Either answer is kept whole: every content block the tool returned, including images, audio and resources, and its structured content. Only unreachable backends and unknown outcomes fail an execution. Draining stops new claims and reconnects while keeping
in-flight execution leases and connections through bounded result persistence.
