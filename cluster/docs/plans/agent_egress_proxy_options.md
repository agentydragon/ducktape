# Agent HTTP egress control plane

## Purpose

Haku Console should control whether an authenticated Agent may initiate an
outbound HTTP(S) connection and whether that Agent may use a Console-managed
credential on the resulting request.

Behind one enforced egress fence, the system must provide:

1. deploy-managed standing destination policy;
2. time-boxed, operator-approved destination grants;
3. destination-scoped credential substitution selected by opaque placeholders;
4. versioned request-scoped API capabilities that can replace custom provider
   backends when their semantics can be expressed safely over HTTP.

Console owns policy and grant state. The adapter and endpoint shape were ruled
on 2026-08-27 ([#4670](https://github.com/agentydragon/ducktape/issues/4670)
§ Decision): mitmproxy embedded as a library, one shared proxy colocated with
Console, and a single Console↔proxy decision call. This document records that
decision and the remaining design.

Tracking issue: [#4670](https://github.com/agentydragon/ducktape/issues/4670).

## Decision record (ruled 2026-08-27)

**mitmproxy is the adapter, embedded as a library.** Its intercepted-HTTP/2
credential substitution is measured (§ Measured mitmproxy behavior) and its
h2/gRPC MITM covers the hardest data-plane must-have: `bbr` → BuildBuddy is
gRPC, so a fence that only tunnels CONNECT cannot serve it. Embedding, rather
than running a `mitmdump` addon script, is what makes fail-closed a property of
our code: stock addon exceptions fail open and `Flow.kill()` is unreliable
mid-stream, whereas owning the process lets an error anywhere in the decision
path terminate the connection.

**The Console↔proxy API is ours to design.** With one adapter there is no
cross-proxy line-protocol compromise, so one decision call — working name
`POST /api/internal/http/decide` (§ Console decision API) — carries both the
reachability verdict and the request-specific credential-substitution
operations, replacing the earlier two-facade `/api/internal/http/authorize` +
`/api/internal/http/credentials/redeem` split. The proxy stays a dumb executor
of Console's instructions, and the hot path makes one round-trip instead of
two.

Rejected adapters, each with the constraint that killed it:

- **iron-proxy**: no per-request hook mechanism, so it cannot ask Console
  anything. It remains today's static fence and retires at the one-proxy end
  state.
- **Squid**: its only differentiator was mature response caching — a v1
  non-goal, since v0 disables response caching entirely — and it lacks h2
  MITM. Its open conformance questions (same-resolution address pinning,
  `client_lifetime` as a security control, helper no-cache semantics) die with
  the spike plan that was to answer them.
- **ICAP as the Console↔proxy seam**: implemented to REQMOD depth
  ([#4043](https://github.com/agentydragon/ducktape/pull/4043), closed
  unmerged — the reference implementation if the constraint landscape ever
  changes) so Console could serve as Squid's adaptation service. The seam
  existed only for Squid and fell with it; the ruled seam is the single
  decide call above — one wire protocol fewer, an internal same-release
  contract instead of an ICAP conformance surface.
- **Envoy**: no dynamic-certificate machinery to reuse, and forward-proxy
  CONNECT interception is still alpha; the earlier spike substituted
  credentials only in reverse-proxy mode. It remains the reference for
  stream-lifetime controls (`max_stream_duration`, `max_connection_duration`).
- **Purpose-built broker**: the fallback for the case where no existing proxy
  could be made conforming. Embedding mitmproxy provides the same process
  ownership without building TLS interception from scratch.

### Measured mitmproxy behavior

From the addon spike
([#4046](https://github.com/agentydragon/ducktape/pull/4046),
[#4051](https://github.com/agentydragon/ducktape/pull/4051)) and upstream
documentation; these constrain the implementation:

- intercepted HTTP/2 requests support credential substitution;
- streaming remains incremental only when enabled before any body access;
- an addon exception forwards the request by default, and `Flow.kill()`
  documents that flows already in transit cannot be killed reliably — the
  embedded adapter therefore needs two independent layers: the
  policy/transformation path and a structural deny backstop that drops every
  flow not affirmatively marked allowed;
- callbacks may need thread-safe scheduling onto mitmproxy's event loop.

## Architecture

```text
Agent workload
  │
  │ HTTP_PROXY / HTTPS_PROXY (convention; Cilium denies
  │ every alternative egress path)
  ▼
shared egress proxy — mitmproxy embedded as a library,
colocated with Console
  ├─ authenticates the caller's Agent-bound fence credential
  ├─ resolves, validates and pins the upstream address
  ├─ intercepts TLS and reads the decrypted request
  ├─ POST /api/internal/http/decide (localhost) → verdict + substitutions
  └─ applies the substitutions and forwards, or terminates
  │
  ▼
upstream permitted by both Console and the proxy pod's Cilium ceiling
(temporary grants remain public-origin-only)
```

### Topology

One shared proxy fences every Agent —
[#4670](https://github.com/agentydragon/ducktape/issues/4670) § Enforcement
topology holds the shared-fence reasoning — and that proxy is colocated with
Console: a sidecar container in the Console pod, or the same container /
in-process with the Console router. The decision endpoint binds on localhost.

- **The oracle constraint becomes structural.** The decision endpoint converts
  placeholder tokens into real credentials, so it must be authenticated,
  reachable only by the proxy and never routable from sandbox workloads.
  Localhost binding inside the Console pod yields sandbox unreachability by
  construction rather than by NetworkPolicy.
- **The API needs no versioning.** Proxy and Console roll as one release unit,
  so the decision call is an internal same-release contract. A separate proxy
  Deployment would need endpoint authentication plus a NetworkPolicy fencing
  the oracle plus a roll-safe versioned contract.
- **The trade, honestly:** a Console roll severs in-flight tunnels, and the
  TLS-interception data plane shares pod CPU with Console. Colocation is the
  default unless data-plane load argues otherwise.

### Trust boundaries

- One proxy fences all Agents, so every fenced pod reaches the one listener and
  caller separation is purely the Agent-bound fence credential. Agent identity
  and access profile derive from authenticating that credential, never from
  request JSON, client IP or proxy-supplied identity fields.
- The Agent pod may reach its proxy and required cluster infrastructure, but may
  not open direct Internet connections. Proxy environment variables are
  convenience, not enforcement.
- The proxy reaches the Console decision endpoint directly on localhost, never
  through itself. No sandbox workload has a route to that endpoint.
- The fence credential is endpoint-scoped: accepted only by the decision
  endpoint, invalid for MCP, Agent session and operator APIs. The general
  `AgentBearerAuthority` has no audience parameter, so this needs a separate
  credential kind and resolver path.
- Console policy is an authority within the proxy pod's Cilium ceiling. It
  cannot override NetworkPolicy, TLS validation or credential-redemption
  restrictions.
- TLS interception uses one deploy-managed shared egress-interception CA
  trusted by Agent workloads behind the fence. The CA private key remains
  available only to the proxy and its provisioning path, never to Agent pods.
  The proxy independently validates the real upstream certificate and fails
  closed; temporary grants never weaken either trust path. Rotation distributes
  the next CA before the proxy begins issuing from it and removes the old trust
  root after a bounded overlap.
- The prohibited sidecar arrangement is the security proxy in the _Agent_ pod:
  those containers share a network namespace, so pod-scoped Cilium enforcement
  cannot separate agent traffic from proxy traffic. Colocating with _Console_
  is the opposite arrangement — Console is trusted.
- Sandbox provisioning fences every workload uniformly; identity arrives with
  the fence credential injected at claim time, and sandbox workloads have no
  authority over the labels that fence them.

## Policy model

### Standing policy and temporary grants

Console evaluates destination access in this order:

1. canonicalize and validate the requested origin and resolved addresses;
2. evaluate the Agent access profile's standing HTTP policy;
3. after a clean standing-policy denial, match active temporary HTTP grants;
4. deny every other outcome, including authority failures and malformed input.

Standing policy and grants share one decision response but remain distinct
sources for audit. The proxy does not cache dynamic authorization decisions.
Approval, release, revocation and expiry therefore affect the next admission.

A denied request does not create an approval as a side effect. Console returns a
machine-readable reason and canonical grant scope; the proxy surfaces a message
such as `temporary HTTP grant required`. The Agent explicitly submits a grant
ToolCall and retries after approval. A future convenience operation may
idempotently ensure a pending request, but the authorization hot path stays
read-only.

### Canonical HTTP origin

Version one grants exact origins, each optionally narrowed by an explicit
method set and a fullmatch path regex evaluated against the request path plus
query ([#4878](https://github.com/agentydragon/ducktape/pull/4878)
`HttpGrantSpec`; ruled in
[#4884](https://github.com/agentydragon/ducktape/issues/4884#issuecomment-5437484931)):

```json
{
  "scheme": "https",
  "host": "files.pythonhosted.org",
  "port": 443
}
```

Normalization rules:

- scheme is lower-case `http` or `https`;
- host is a lower-case IDNA A-label with no trailing dot;
- port is explicit after applying the scheme default;
- IP literals are rejected;
- wildcard domains and host regular expressions are not grant predicates.

A redirect chain needs every origin in one atomic grant set. Method and path
predicates carry authority only where the adapter demonstrably sees the inner
decrypted request: a CONNECT tunnel the proxy cannot decrypt has no path, so
path-scoped coverage applies to intercepted requests while opaque-tunnel
reachability stays host-and-port-scoped by construction.

### Request-scoped API capabilities

Exact-origin grants are the safe first implementation, but they are not the
control plane's final abstraction. Once an adapter proves decrypted-request
visibility and upstream-equivalent request parsing, Console may authorize a
versioned canonical API operation such as:

```json
{
  "origin": {
    "scheme": "https",
    "host": "gmail.googleapis.com",
    "port": 443
  },
  "method": "GET",
  "operation": "gmail.users.messages.get",
  "path_parameters": {
    "user_id": "me",
    "message_id": "18f2a4..."
  },
  "query": {
    "format": "metadata"
  }
}
```

This makes the egress fence a reusable capability gateway and a first-class
Agent-facing interface. For a simple API, the deployment may expose only the
upstream HTTP contract, one or more opaque credential placeholders and concise
Agent instructions. This can be less code and consume fewer model-context tokens
than mounting a provider-specific MCP server. Where schemas, pagination,
response compaction or discoverability justify them, a provider-specific MCP
implementation may instead become a generated or thin tool surface over the
same HTTP executor, authorization and credential-redemption machinery. Typed
tools are optional ergonomics, not a required architectural layer.

Request rules are structured, deploy-reviewed data. They are not arbitrary
caller-supplied regular expressions. Each backend policy may have bespoke
normalization and matching code, analogous to the existing per-MCP-backend
autoapproval policies, while sharing the gateway's authentication, lifecycle,
audit and credential machinery. A route template may use a narrowly typed
parameter matcher internally, but Console records and approves the normalized
operation and constraints.

The first request-capability implementation does not need a general provider
schema or sophisticated per-object protection. It may use a small reviewed
method/route/query allowlist for one backend and improve filtering when a real
need appears. In particular, a pattern that merely recognizes the syntax of a
Gmail message ID authorizes **every** matching message ID; it does not mean one
exact message. That breadth is acceptable when the Agent's standing or
autoapproved policy already permits all Gmail reads. Exact IDs and bounded sets
remain available for narrower future policies, but they are not the default
calibration for such an Agent.

A conforming request-scoped adapter must:

- authenticate the same Agent-bound fence credential used for origin admission;
- bind the request decision to an already allowed canonical origin and pinned
  upstream address;
- parse the request target exactly as the upstream service will, rejecting
  ambiguous encodings, conflicting authority fields, userinfo, fragments,
  encoded separators and normalization mismatches;
- match an explicit method and provider operation or route template;
- treat the query as a normalized multimap with allowlisted keys, cardinality
  and value constraints rather than as an unparsed string;
- reject method-override headers and reauthorize every redirect;
- pass only bounded policy metadata to Console, never arbitrary bodies or
  unrelated headers;
- bind credential redemption to the request authorization decision so a token
  approved for one operation cannot be replayed on another; and
- fail closed when the adapter cannot classify a request unambiguously.

The first request-scoped version remains body-independent. Read-only APIs and
operations whose authority is fully determined by method, route and query are
the best candidates. Mutations whose meaning lives in JSON, protobuf, multipart
or upload bodies need a provider schema, an exact body hash, or a dedicated
typed adapter before they can use this path. Response authorization is also
separate: granting a request grants the complete upstream response unless a
provider adapter explicitly performs size, media-type or field projection.

Candidate migrations illustrate the boundary:

- **Grocy:** its conventional REST surface and API-key presentation make it a
  strong candidate for a reviewed route allowlist presented directly to the
  Agent through instructions and an opaque placeholder. OpenAPI-derived rules
  or generated tools remain options when their validation and ergonomics repay
  their code and context cost. Read operations can move first; writes require
  explicit operation and body schemas.
- **Gmail:** Google Discovery operation IDs can define a reviewed subset such as
  `gmail.users.messages.list` or `gmail.users.messages.get`. Console keeps the
  refresh credential, injects a transient least-scope OAuth access token and
  constrains route and query fields such as `format`. An Agent whose standing
  policy already permits all Gmail reads may receive broad list/get coverage
  rather than per-message grants. Listing a mailbox, fetching one exact message
  and fetching any syntactically valid message ID remain distinguishable for
  narrower policies. Send, modify, batch, upload and
  attachment operations remain excluded until their body and response behavior
  is modeled.
- **Kubernetes:** the shared fence and credential machinery may carry direct
  Kubernetes API traffic, but generic URL rules must not replace the existing
  typed Kubernetes authorization domain. Kubernetes paths require discovery,
  resource versus non-resource classification, namespace and cluster-scope
  disambiguation, subresources, selectors and upgrade semantics. The proxy
  should continue projecting each request into canonical `RequestAttributes`
  and calling the Kubernetes SAR/grant authorizer. This can replace a bespoke
  transport wrapper, not the semantic matcher or grant model.

After exact-origin, provider-operation and Kubernetes domains provide enough
evidence, their common ownership, approval, lifetime, audit and credential
binding may be extracted into one capability-grant envelope. The enclosed
matcher remains typed: HTTP origin, provider operation and Kubernetes
`RequestAttributes` have different coverage and canonicalization rules. This can
unify the control plane without pretending that one URL-regex language safely
replaces every domain.

This extension should be implemented only after exact-origin grants establish
the shared lifecycle, identity, DNS and credential foundations. It is a
versioned sibling matcher, not a reason to weaken v1 origin rules or begin with
one untyped URL-regex grant table.

### Public-address restriction and DNS pinning

Temporary grants are public-origin-only. Internal destinations remain
standing-policy-only until a separately reviewed scope exists.

For every new upstream connection the adapter:

1. resolves the canonical host;
2. supplies the bounded complete address set and selected address to Console;
3. receives authorization for that exact origin and resolution;
4. connects to the selected address without resolving the hostname again.

Reconnects repeat the sequence. Console rejects an address set containing any
prohibited address, including:

- IPv4 loopback, RFC 1918, carrier-grade NAT and link-local ranges;
- IPv6 loopback, ULA and link-local ranges;
- cloud metadata endpoints;
- Kubernetes Pod and Service CIDRs;
- cluster node and host addresses;
- unspecified, multicast, reserved and otherwise non-public addresses.

Rejecting the whole mixed answer set avoids selection ambiguity. Conformance
tests cover IPv4, IPv6, mixed answers, changed answers and DNS rebinding.

The current proxy Cilium policy contains exact public FQDN rules and a broad
`toEntities: cluster` rule for in-cluster traffic. That policy does not itself
prove private-address denial. The adapter and Console checks remain required.

### Cilium ceiling

A temporary grant cannot rewrite NetworkPolicy. The proxy may connect only where
both layers allow it:

```text
Console standing policy or temporary grant
                    AND
proxy-pod Cilium egress policy
```

With an exact static FQDN ceiling, a grant can select only an already admitted
origin. The intended end state instead gives the proxy pod a reviewed
public-Internet ceiling on explicit proxy ports while the Agent pod remains
restricted to its proxy. Console then becomes the main destination boundary,
with the proxy's public-address, DNS-pinning and fail-closed checks forming part
of that authority. The Cilium ceiling still excludes private, cluster, metadata
and otherwise prohibited destinations and prevents every bypass around the
proxy. This widening is a separate GitOps review, not a grant side effect.

## HTTP grant domain

HTTP grants should reuse the implemented Kubernetes grant lifecycle as a sibling
typed domain, not add URL semantics to `KubernetesGrantService`.

A creation request groups one or more exact origins under one lifetime:

```json
{
  "grants": [
    {
      "destinations": [
        { "scheme": "https", "host": "pypi.org", "port": 443 },
        { "scheme": "https", "host": "files.pythonhosted.org", "port": 443 }
      ]
    }
  ],
  "duration_seconds": 3600
}
```

Each persisted grant records:

- durable grant ID;
- owning Agent ID;
- immutable source ToolCall ID;
- exact normalized destination set;
- created and expiry timestamps;
- active, released, revoked or expired status;
- terminal reason and timestamp.

### Source and ownership invariant

Creation succeeds only when `source_tool_call_id` identifies the same Agent's
manually approved `http_access/create_grant` ToolCall:

- the source call is authenticated through that Agent's credential binding;
- server and tool names match exactly;
- status is `running` or `ok`;
- `approved_at` is present;
- `approval_policy_id` is absent, so an auto-approved call cannot mint a grant.

One source ToolCall idempotently owns one atomic grant set. This is enforced in
the repository transaction, not left to an MCP wrapper convention.

### Lifecycle

The domain provides create, list, get, release and operator revocation. Reads are
Agent-scoped. Release and revocation are terminal and idempotent. Expiry uses the
same durable expiry/reaper pattern as Kubernetes grants.

The decision response carries the matched grant's earliest expiry as
`valid_until`. Standing-policy decisions omit it unless the standing rule itself
has a deadline.

### Admission expiry and active-flow lifetime

Version one treats `valid_until` as an exact admission deadline:

- no new HTTP request, CONNECT, reconnect or upgraded flow begins after expiry;
- release and revocation deny the next admission immediately;
- proxy-local authorization caching is disabled.

Version one does not require every byte of an admitted connection to stop at the
exact grant expiry. The deployment imposes a small hard `max_flow_lifetime`, so
an already admitted flow may overrun expiry or revocation only by that fixed
amount. Protocols requiring unbounded WebSockets, downloads or tunnels do not
use temporary grants; they need standing policy or a later adapter extension
with exact per-flow deadline and revalidation support.

Exact termination at `valid_until` may be added later as a stronger property;
it is not a v1 requirement.

## Console-owned credential handles

Console owns provider credentials, opaque placeholders and redemption policy.
The Agent never receives the real credential.

### Credential model

An egress credential handle binds:

- a deterministic, inert placeholder string such as `github-token-placeholder`;
- one provider credential secret reference;
- the Agent or Agents allowed to select it;
- exact allowed origins;
- supported presentation forms such as Bearer or Git-over-HTTPS Basic;
- active lifetime and revocation state;
- audit-safe label such as `github-personal` or `github-bot`.

The placeholder is not a secret
([#4884](https://github.com/agentydragon/ducktape/issues/4884#issuecomment-5437501221)):
it is worthless against external services, loggable, and committed in sandbox
templates by design; only the redeemed real value is never-log material.
Possession grants nothing. Copying a placeholder to another Agent is
insufficient because redemption is bound to the Agent identity authenticated
from the fence credential, and presenting it at the wrong origin never causes
substitution. Rotating the provider credential happens entirely Console-side;
the placeholder need not change.

An Agent may receive several named placeholders for the same provider. This
allows deliberate account selection, for example:

```text
GITHUB_PERSONAL_PLACEHOLDER → personal GitHub credential
GITHUB_BOT_PLACEHOLDER      → bot GitHub credential
```

The Agent selects an account by using the corresponding placeholder. Version
zero permits anonymous access and independently supplied credentials to pass
through. Agents are not expected to possess such credentials, so this does not
need to block the initial deployment. The proxy exports a counter whenever a
managed destination and credential slot would support Console substitution but
receives a different credential. The metric records Agent, destination, slot
and policy disposition without recording the credential value.

The policy model leaves room for a per-binding or per-destination credential
mode:

- `passthrough`: preserve independently supplied credentials;
- `managed`: replace the recognized placeholder but otherwise pass through;
- `force_managed`: replace any supplied value with the assigned Console
  credential; and
- `deny_unmanaged`: reject supplied credentials other than recognized
  placeholders.

Version zero defaults to `managed`; stricter modes can be added without changing
the placeholder or redemption contract.

### Separate reachability and credential authority

Destination admission and credential redemption are independent authorities.
One decision call carries both outcomes (§ Console decision API), but they are
evaluated separately:

- a reachability grant does not unlock every credential valid for that origin;
- a credential-use grant cannot expand HTTP or Cilium reachability;
- a manually approved operation may create both authorities atomically as a
  convenience bundle, but they remain separate typed records and matchers;
- standing destination policy and standing credential assignments follow the
  same separation.

Version one credential rules use exact origins. A later request-scoped matcher
may additionally bind redemption to a canonical provider operation and request
decision. Path and method strings alone are insufficient without the parsing,
query and ambiguity requirements above.

### Audit and secret handling

The deployment preserves a full-fidelity outbound request audit trail so an
operator can reconstruct exactly what an Agent attempted. For decrypted HTTP it
includes the authenticated Agent, timestamp, canonical origin, selected
upstream address, method, original normalized path and query, relevant
non-secret headers, matched backend operation and policy, redirect chain,
credential disposition, decision IDs, response status, byte counts and timing.
Denied and malformed requests are retained as well as successful requests.

These records are intentionally sensitive and belong in a Console-controlled,
access-restricted audit store with explicit retention and deletion policy. They
must not be emitted to ordinary proxy logs, metrics or Agent-visible errors.
Credential values, cookies, authorization headers and other configured secret
fields are always redacted; placeholders are inert and need no redaction.
Request or response body capture is not required for the first implementation;
a backend may add separately protected, size-bounded body capture when exact
mutation debugging justifies its additional sensitivity.

Console returns the real credential value only inside a per-request
substitution, not as a reusable provider credential object. The adapter:

- keeps real credential values out of ordinary and audit logs and metrics (the
  placeholder itself is loggable by design);
- does not write real values into generated configuration, files or cache;
- holds real-value bytes only for the request transformation;
- applies exactly the returned substitutions, scanning only the named headers;
- forwards placeholder occurrences the substitutions do not cover verbatim —
  no stripping and no unsubstituted-placeholder special case
  ([#4884](https://github.com/agentydragon/ducktape/issues/4884#issuecomment-5437501221));
- performs no destination or Agent scoping of its own: decisions are
  per-request, so Console returns a substitution only for a request it has
  already checked against destination and Agent policy.

Version zero disables HTTP response caching entirely: the proxy is a
non-caching forwarder, verified to store neither authenticated nor anonymous
responses. This removes cache-key, privacy and secret-persistence questions
from the authorization milestone; credential and authorization values are never
persisted regardless of whether a later response-cache design is added. Any
later cache proposal must separately define cache keys after normalization,
privacy boundaries, authenticated and credential-substituted exclusions,
response directives, size limits and purge behavior. This is independent of the
prohibition on proxy-local authorization-decision caching, which is a permanent
control-plane requirement.

## Console decision API

One authenticated call carries the reachability verdict and the
request-specific credential-substitution operations together. This section is
the current proposal — the working shape the implementation
([#4884](https://github.com/agentydragon/ducktape/issues/4884)) will pin; the
substitution semantics are ruled and already implemented proxy-side
([#4876](https://github.com/agentydragon/ducktape/pull/4876)). It is an
internal same-release contract (§ Topology): proxy and Console deploy from one
commit, so there is no version negotiation and no versioning.

```text
POST /api/internal/http/decide        (bound on localhost)
Authorization: Bearer <proxy identity credential>
```

The call is authenticated even though it is localhost-bound: the oracle
constraint — authenticated, reachable only by the proxy, never routable from
sandbox workloads — is defense in depth, not either/or.

The request carries the caller's fence credential, the canonical origin, the
pinned resolution and the request metadata that grant coverage is evaluated
against:

```json
{
  "fence_credential": "<Agent-bound fence credential>",
  "origin": { "scheme": "https", "host": "api.github.com", "port": 443 },
  "resolved_ips": ["140.82.121.5", "2a01:4f8:c010::5"],
  "upstream_ip": "140.82.121.5",
  "method": "GET",
  "path": "/repos/agentydragon/ducktape/pulls?state=open"
}
```

- Console derives the Agent and access profile by authenticating
  `fence_credential`; the request carries no caller-asserted `agent_id`.
- `method` and `path` — the path plus query exactly as the proxy will send it —
  travel because grant coverage is evaluated against the concrete request:
  host, port, method set, path regex
  ([#4878](https://github.com/agentydragon/ducktape/pull/4878)
  `HttpGrantSpec`; ruled in
  [#4884](https://github.com/agentydragon/ducktape/issues/4884#issuecomment-5437484931)).
  A CONNECT the proxy cannot decrypt has no path, so path-scoped coverage
  applies to intercepted requests while opaque tunnels stay host:port-scoped.
  The client-controlled Host header is never policy input.
- Header values and bodies stay out of the call. The placeholder the sandbox
  holds is inert (§ Credential model), so nothing about it needs to travel
  inward — grant evaluation alone decides which substitutions come back.
  Bodies can carry sensitive application data, and Console sits on the
  request-decision path, never the body path.

The allowed response carries the verdict, its audit linkage, the admission
lifetime and the substitutions for this one request:

```json
{
  "allowed": true,
  "source": "grant",
  "decision_id": "grant:018f...",
  "valid_until": "2026-08-26T02:30:00Z",
  "substitutions": [
    {
      "placeholder": "github-token-placeholder",
      "value": "<real credential value>",
      "match_headers": ["authorization"]
    }
  ]
}
```

Denied response:

```json
{
  "allowed": false,
  "source": "none",
  "reason": "temporary_grant_required",
  "grant_scope": { "scheme": "https", "host": "api.github.com", "port": 443 }
}
```

Semantics:

- The proxy calls decide for every admission — each decrypted request, each
  CONNECT it cannot decrypt, each reconnect — with proxy-local decision caching
  disabled, so approval, release, revocation and expiry affect the next
  admission. CONNECT, decrypted HTTP, WebSocket upgrade and HTTP/2 stream hooks
  map to the same origin semantics.
- Reachability and credential redemption remain independent authorities inside
  the one call: holding a placeholder never expands reachability, and
  reachability never unlocks credentials. On a route where no substitution
  scope matches, the decision is either deny — grant policy — or allow with
  the placeholder passing through verbatim; the placeholder is inert upstream,
  the proxy performs no stripping and has no unsubstituted-placeholder special
  case, and allow-without-substitutions is a normal outcome
  ([#4884](https://github.com/agentydragon/ducktape/issues/4884#issuecomment-5437501221)).
- A substitution is placeholder → real value plus the headers to scan —
  `{placeholder, value, match_headers}`, proxy-side `PlaceholderSubstitution`
  in `haku/egress/decision.py`
  ([#4876](https://github.com/agentydragon/ducktape/pull/4876)). The proxy
  replaces each occurrence of the placeholder inside the scanned header
  values, reaching inside the base64 payload of Basic credentials — the shape
  git over HTTPS sends — and touches nothing else; a request that never
  presents the placeholder is forwarded untouched and receives no credential.
  The response model marks real values secret; they are never logged or
  persisted on either side, while placeholders are loggable by design.
- `valid_until` is an exact admission deadline (§ Admission expiry and
  active-flow lifetime); an already admitted flow may overrun it only within
  the deployment-wide hard `max_flow_lifetime`.
- `decision_id` links the restricted audit stream's request records to their
  policy provenance.

## Conformance properties

Requirements on the mitmproxy implementation, proven by conformance tests that
any future replacement must also pass. The implementation must:

1. authenticate to Console without caller-selectable Agent identity;
2. call the decision endpoint for every admission with proxy-local decision
   caching disabled;
3. fail closed on timeout, malformed response, denied decision or authority
   failure — an error anywhere in the decision path terminates the connection,
   never forwards;
4. validate and pin DNS resolution for the actual upstream connection;
5. validate upstream TLS independently and issue intercepted certificates only
   from the deploy-managed shared egress-interception CA;
6. enforce the deployment's finite hard flow lifetime;
7. surface a useful denial reason without exposing credentials or policy
   internals;
8. take the decision call's request metadata — method, scheme, host, port,
   path with query — from the connection target and decrypted request, never
   from the client-controlled Host header;
9. apply exactly the returned substitution operations and never persist them;
10. forward placeholder occurrences the substitutions do not cover verbatim,
    with no stripping and no unsubstituted-placeholder special case;
11. keep header values and bodies out of decision calls while writing exact
    non-secret request metadata to the restricted audit stream independently
    of policy evaluation.

## Failure behavior

The system denies on:

- missing, invalid, expired or wrong-audience fence or proxy credentials;
- Console timeout or unavailability — denying all external HTTP including
  standing destinations, because Console is authoritative and the proxy holds
  no policy copy;
- malformed or incomplete decision responses;
- destination denial or missing grants;
- DNS resolution failure, oversized answers or prohibited addresses;
- mismatch between selected and connected address;
- credential substitution failure;
- inability to enforce the configured hard flow lifetime.

The authorization path never waits for an operator. Retries are explicit and
normal. Logs distinguish policy denial, unavailable authority and malformed
adapter behavior without exposing sensitive values.

## Implementation sequence

1. Define `HttpGrant`, exact-origin normalization and coverage tests.
2. Add persistence, migration, repository provenance checks and lifecycle
   service by following the Kubernetes grant implementation.
3. Add Agent create/list/get/release tools and operator inspection/revocation.
4. Define deploy-managed standing destination policy.
5. Mint endpoint-scoped Agent-bound fence credentials and a dedicated resolver.
6. Implement `POST /api/internal/http/decide` as a read-only reachability
   facade with an empty substitution set.
7. Define Console-owned egress credential handles and credential-use grants.
8. Extend the decide response with substitution operations and mandatory
   secret redaction.
9. Decide the proxy-pod Cilium ceiling for grantable public origins.
10. Embed mitmproxy as a library adapter colocated with Console (§ Topology)
    and prove the conformance properties, including the structural deny
    backstop, by test.
11. Run end-to-end grant, credential-selection, failure and rebinding tests.
12. Roll out one experimental Agent before generalizing the deployment.
13. Define a versioned structured request-capability model and bind credential
    redemption to its decision IDs.
14. Expose one read-only Grocy or similarly conventional API directly through
    instructions and an opaque placeholder; add generated tools only if their
    ergonomics justify the additional surface.
15. Model broad Gmail list/get coverage for an Agent whose existing policy
    allows all Gmail reads, using Google Discovery operation IDs, least-scope
    OAuth tokens and explicit route/query constraints.
16. Route direct Kubernetes HTTP through the existing canonical
    `RequestAttributes` authorizer without replacing typed Kubernetes grants
    with generic URL rules.

## Acceptance tests

### Destination grants

- standing origin succeeds without a temporary grant;
- unknown origin denies with the exact canonical grant scope;
- manually approved grant succeeds on retry;
- the same grant fails for another Agent or origin;
- redirect succeeds only when every origin is covered;
- expiry, release and revocation deny later admissions;
- an existing flow cannot exceed `max_flow_lifetime`;
- Console timeout, invalid JSON and an invalid fence or proxy-identity
  credential deny;
- proxy-local decision caching is absent.

### Address safety

- public IPv4 and IPv6 answers connect to the selected validated address;
- loopback, private, carrier-grade NAT, link-local, metadata, ULA, cluster,
  Service and node addresses deny;
- a mixed public/private answer denies;
- DNS changing between requests is re-evaluated;
- the adapter never performs a second unvalidated resolution before connect.

### Credential selection

- each GitHub-account placeholder selects only its own credential;
- another Agent cannot redeem a copied placeholder;
- the right placeholder at the wrong origin does not substitute;
- version-zero requests without placeholders remain anonymous or preserve
  independently supplied credentials;
- a managed credential slot containing an independently supplied credential
  increments the audit-safe unexpected-credential metric without exposing its
  value;
- an allowed request whose route matches no substitution scope, or a
  placeholder occurrence outside the scanned headers, passes through verbatim
  rather than being stripped or denied;
- Bearer and Git-over-HTTPS Basic forms substitute correctly, including inside
  the base64 Basic payload;
- request bodies and redeemed real values are absent from logs, metrics and
  cache;
- credential revocation affects the next request independently of reachability.

### Request capability enforcement

- ambiguous encodings, conflicting authority, method overrides and unclassified
  routes deny;
- an exact resource ID does not cover another ID;
- a typed parameter-pattern scope is visibly broader than an exact-ID scope;
- query keys, duplicate values and formats outside the approved constraint deny;
- credential redemption is bound to the matching request decision;
- redirects reauthorize both destination and request operation;
- read-only capabilities cannot reach mutation, batch, upload or streaming
  operations;
- bodies and unrelated headers are absent from Console policy calls;
- the restricted audit stream preserves exact path/query activity and decision
  provenance while redacting credential, cookie and configured secret values;
- provider responses are size-bounded, or the capability explicitly documents
  that it returns the complete upstream representation;
- Kubernetes requests are authorized from canonical `RequestAttributes`, not a
  URL regex.

### Fence enforcement

- removing proxy environment variables does not create direct egress;
- the decision endpoint is unreachable from every sandbox workload;
- a provisioned sandbox is fenced uniformly; its identity comes only from its
  injected fence credential, never from network position;
- removing the proxy fails closed rather than exposing a fallback path;
- the Agent cannot mutate the labels selecting its fence.

## Related work

- [#3898](https://github.com/agentydragon/ducktape/pull/3898): initial proxy survey
- [#4023](https://github.com/agentydragon/ducktape/pull/4023): Console-authoritative decisions and per-Agent fences
- [#4031](https://github.com/agentydragon/ducktape/pull/4031),
  [#4036](https://github.com/agentydragon/ducktape/pull/4036), and
  [#4037](https://github.com/agentydragon/ducktape/pull/4037): Squid and ICAP experiments
- [#4038](https://github.com/agentydragon/ducktape/pull/4038): Console credential ownership
- [#4046](https://github.com/agentydragon/ducktape/pull/4046) and
  [#4051](https://github.com/agentydragon/ducktape/pull/4051): Squid and mitmproxy comparison
- [#4113](https://github.com/agentydragon/ducktape/pull/4113): temporary Kubernetes grants
- <../../../haku/kube_api_proxy/README.md>
- <../../../docs/personal_agents/credential_proxy.md>

## References

- <https://docs.mitmproxy.org/stable/api/events.html>
- <https://github.com/mitmproxy/mitmproxy/blob/main/mitmproxy/flow.py>
- <https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/core/v3/protocol.proto>
- <https://developers.google.com/workspace/gmail/api/reference/rest>
- <https://kubernetes.io/docs/reference/access-authn-authz/authorization/>
- <https://agentgateway.dev/blog/2026-07-27-credential-injection-ai-agent-egress-cb4a/>
- <https://infisical.com/blog/agent-proxy>
- <https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy>
