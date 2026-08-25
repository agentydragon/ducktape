# Agent HTTP egress control plane

## Purpose

Haku Console should control whether an authenticated Agent may initiate an
outbound HTTP(S) connection and whether that Agent may use a Console-managed
credential on the resulting request.

The system must provide five capabilities behind one enforced egress fence:

1. deploy-managed standing destination policy;
2. time-boxed, operator-approved destination grants;
3. destination-scoped credential substitution selected by opaque placeholders;
4. versioned request-scoped API capabilities that can replace custom provider
   backends when their semantics can be expressed safely over HTTP.

Console owns policy and grant state. The concrete proxy is replaceable. Squid is
the first implementation candidate because it has already demonstrated TLS
interception, credential substitution and the controls needed to disable unsafe
caching, but Squid helper and ICAP protocols are adapters rather than
control-plane contracts.

Tracking issue: [#4670](https://github.com/agentydragon/ducktape/issues/4670).

## Architecture

```text
Agent workload
  │
  │ HTTP_PROXY / HTTPS_PROXY
  │ Cilium denies every alternative egress path
  ▼
per-Agent proxy pod
  ├─ authenticates to Console with an Agent-bound proxy credential
  ├─ asks Console about destination admission
  ├─ for request-scoped capabilities, asks about the canonical API operation
  ├─ asks Console about recognized credential placeholders
  ├─ validates and pins the selected upstream address
  └─ optionally intercepts TLS and substitutes credentials
  │
  ▼
upstream permitted by both Console and Cilium
(temporary grants remain public-origin-only)
```

### Trust boundaries

- Each Agent has a separate proxy deployment and pod. The Agent pod is not
  co-located with the proxy because Cilium enforcement is pod-scoped.
- The Agent pod may reach its proxy and required cluster infrastructure, but may
  not open direct Internet connections. Proxy environment variables are
  convenience, not enforcement.
- The proxy pod may reach the Console authorization Services directly without
  sending those calls through itself. Agent workloads do not inherit that route.
- The proxy authenticates with a Console-minted credential bound to one Agent and
  narrow internal HTTP-policy audiences. Request JSON, client IP and proxy fields
  never select `agent_id` or access profile.
- Console policy is an authority within the proxy pod's Cilium ceiling. It cannot
  override NetworkPolicy, TLS validation or credential-redemption restrictions.
- TLS interception uses one deploy-managed shared egress-interception CA trusted
  by Agent workloads behind the fence. The CA and private key remain available
  only to the proxy deployment and its provisioning path, never to Agent pods.
  Each proxy independently validates the real upstream certificate and fails
  closed; temporary grants never weaken either trust path. Rotation distributes
  the next CA before proxies begin issuing from it and removes the old trust root
  after a bounded overlap.
- A proxy deployment may contain helper containers or processes. The prohibited
  sidecar arrangement is placing the security proxy in the Agent workload pod.

### Why one proxy per Agent

A shared proxy would require reliable caller attribution across every protocol
and would combine credential blast radii. A per-Agent proxy makes the fence
identity deploy-managed and constant:

- one Agent-bound Console credential per proxy;
- one Cilium-selected fence per Agent;
- one credential transit boundary per Agent;
- fail-closed lifetime coupling: deleting the proxy removes the Agent's external
  egress rather than exposing a fallback path.

Sandbox provisioning derives the proxy selection from the authenticated Agent.
The caller cannot request a different Agent's fence. Workload labels select the
fence and sandbox workloads have no authority to change those labels.

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

Version one grants exact origins:

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
- wildcard domains, regular expressions, paths and methods are not v1 grant
  predicates.

A redirect chain needs every origin in one atomic grant set. Method and path
predicates may be added only for an adapter mode that demonstrably sees the
inner decrypted request. Bare CONNECT authorization cannot claim those
semantics.

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
same HTTP executor, authorization and credential-redemption services. Typed
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

- authenticate the same Agent-bound proxy credential used for origin admission;
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

The authorization service returns the matched grant's earliest expiry as
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
use temporary grants; they need standing policy or a later adapter with exact
per-flow deadline and revalidation support.

A proxy that can terminate at `valid_until` may provide that stronger property,
but it is not a portable v1 requirement.

## Console-owned credential handles

Console owns provider credentials, opaque placeholders and redemption policy.
The Agent never receives the real credential.

### Credential model

An egress credential handle binds:

- a high-entropy opaque placeholder, stored by fingerprint;
- one provider credential secret reference;
- the Agent or Agents allowed to select it;
- exact allowed origins;
- supported presentation forms such as Bearer or Git-over-HTTPS Basic;
- active lifetime and revocation state;
- audit-safe label such as `github-personal` or `github-bot`.

The placeholder is not a predictable database ID. Console generates it with a
cryptographically secure random source, persists only its fingerprint, and
returns the raw value once through the deploy-managed Agent provisioning or
secret-delivery path. Rotation creates a new handle and bounded overlap before
revoking the old one; there is no API for reading a raw placeholder later.
Copying it to another Agent is insufficient because Console also authenticates
the proxy's Agent identity. Presenting it at the wrong origin never causes
substitution.

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

Destination admission and credential redemption are independent decisions:

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
Credential and placeholder values, cookies, authorization headers and other
configured secret fields are always redacted. Request or response body capture
is not required for the first implementation; a backend may add separately
protected, size-bounded body capture when exact mutation debugging justifies its
additional sensitivity.

Console returns only the exact rendered replacement required for one request,
not a reusable provider credential object. The adapter:

- keeps placeholder and replacement values out of ordinary and audit logs and
  metrics;
- does not write replacements into generated configuration, files or cache;
- holds replacement bytes only for the request transformation;
- strips a recognized placeholder if redemption fails, then denies the request;
- never substitutes based on placeholder match alone without destination and
  Agent checks.

Version zero disables HTTP response caching entirely. This removes cache-key,
privacy and secret-persistence questions from the authorization milestone.
Credential and authorization values are never persisted regardless of whether a
later response-cache design is added.

## Stable Console APIs

### Destination authorization

```text
POST /api/internal/http/authorize
Authorization: Bearer <Agent-bound proxy credential>
```

Request:

```json
{
  "scheme": "https",
  "host": "files.pythonhosted.org",
  "port": 443,
  "resolved_ips": ["151.101.0.223", "2a04:4e42::223"],
  "upstream_ip": "151.101.0.223"
}
```

Allowed response:

```json
{
  "allowed": true,
  "source": "grant",
  "decision_id": "grant:018f...",
  "valid_until": "2026-08-26T02:30:00Z"
}
```

Denied response:

```json
{
  "allowed": false,
  "source": "none",
  "reason": "temporary_grant_required",
  "grant_scope": {
    "scheme": "https",
    "host": "files.pythonhosted.org",
    "port": 443
  }
}
```

This endpoint authorizes an origin admission. It deliberately omits HTTP method,
path and body. CONNECT, decrypted HTTP, WebSocket upgrade and HTTP/2 stream hooks
must map to the same origin semantics.

### Credential redemption

```text
POST /api/internal/http/credentials/redeem
Authorization: Bearer <Agent-bound proxy credential>
```

The v1 request contains only the canonical origin, the recognized credential
slot/presentation kind and the opaque placeholder. It omits method, path, query
and body because none participates in v1 authority and each can contain
sensitive application data. A representative shape is:

```json
{
  "scheme": "https",
  "host": "api.github.com",
  "port": 443,
  "presentation": {
    "kind": "bearer",
    "slot": "authorization",
    "placeholder": "haku-egress-placeholder-v1-..."
  }
}
```

An allowed response carries an audit decision ID and the exact replacement for
the credential slot. The response model marks replacement fields as secret and
all access logging redacts them.

The proxy credential is accepted only for `http-authorize` and
`http-credential-redeem`; it is invalid for MCP, Agent session and operator APIs.
The current general `AgentBearerAuthority` has no audience parameter, so the HTTP
implementation needs a separate credential kind and resolver path rather than a
copy of the Kubernetes proxy route.

## Proxy adapter contract

Every supported proxy adapter must demonstrate the same conformance properties:

1. authenticate to Console without caller-selectable Agent identity;
2. call destination authorization for every admission with proxy-local decision
   caching disabled;
3. fail closed on timeout, malformed response, denied decision or authority
   failure;
4. validate and pin DNS resolution for the actual upstream connection;
5. validate upstream TLS independently and issue intercepted certificates only
   from the deploy-managed shared egress-interception CA;
6. enforce the deployment's finite hard flow lifetime;
7. surface a useful denial reason without exposing credentials or policy internals;
8. call credential redemption only after a recognized placeholder is present and
   decrypted request metadata is available;
9. apply only the returned credential replacement and never persist it;
10. deny on credential-adapter failure rather than forwarding the placeholder;
11. keep method, path, query and body out of v1 Console authorization and
    redemption calls while writing exact non-secret request metadata to the
    restricted audit stream independently of policy evaluation.

Proxy-specific protocols remain implementation details:

| Proxy     | Admission hook                      | Credential transformation                    |
| --------- | ----------------------------------- | -------------------------------------------- |
| Squid     | `external_acl_type` helper          | REQMOD adapter or another tested helper path |
| mitmproxy | addon request/connection hooks      | addon request-header transformation          |
| Envoy     | `ext_authz` HTTP or gRPC            | requires a compatible decrypted-request path |
| custom    | native call before upstream connect | native request-header transformation         |

## First adapter: Squid composition

Squid remains the first candidate because the existing spike proves the hardest
combined data-plane properties: dynamic TLS certificates, destination-scoped
Bearer and Basic substitution, and reliable authenticated-response cache denial.

### Admission helper

A concurrent `external_acl_type` helper maps version-verified Squid origin and
address fields to the stable Console JSON request and maps an affirmative
response to `OK`; denial and every internal failure map to `ERR`. Client address
is never identity. Security does not depend on Squid's `BH` behavior.

The spike must select the exact helper format tokens supported by the deployed
Squid version and prove that they describe the same selected upstream address
used for the connection. The exact no-cache spelling must also be verified.
If `ttl=0` does not disable the result cache, the adapter needs another mechanism
before it is conforming.

The helper is never asked to create grants or wait for human approval. A denied
request returns immediately. Console downtime denies all external HTTP,
including standing destinations, because Console is authoritative.

### Credential transformation

`external_acl_type` cannot rewrite arbitrary origin headers. Squid therefore
needs a separate transformation adapter. REQMOD is one candidate, but the stable
credential contract is ordinary Console JSON, not ICAP.

A proxy-local REQMOD adapter can receive Squid's request, extract only canonical
metadata and the placeholder, call Console redemption, and apply the returned
replacement. It must not forward the body to Console. A mitmproxy replacement
would implement the same JSON contract directly in an addon.

Static Squid `request_header_replace` rules remain useful only as experimental
evidence. They are not the target because generated per-Agent credential config
would put real secrets at rest in the proxy.

### DNS and address enforcement

The admission helper alone cannot prove that Squid connects to the same address
Console checked. The spike must determine whether Squid destination ACLs and its
DNS cache can provide a same-resolution guarantee. A conforming composition
must deny prohibited address classes and pin the validated selected address. If
Squid cannot demonstrate this, use a tested relay/broker or another proxy rather
than weakening the requirement.

### Flow lifetime

Squid documents `client_lifetime` as the maximum time a client may remain
connected to the cache process. Configure a small value and test it against an
active CONNECT tunnel. Reconnection must cause a fresh Console authorization.

The directive is documented as socket-resource protection rather than a security
control, so the test is load-bearing. If it does not bound active tunnels, move
to another existing stream-lifetime primitive or compose a small broker.

### Response caching

Response caching is deferred. Version zero configures the selected proxy as a
non-caching forwarder and verifies that neither authenticated nor anonymous
responses are stored. This is independent of the prohibition on proxy-local
authorization-decision caching, which is a permanent control-plane requirement.

A later cache proposal must separately define cache keys after normalization,
privacy boundaries, authenticated and credential-substituted exclusions,
response directives, size limits and purge behavior. It is not part of the
initial grant or capability-gateway implementation.

## Existing implementation options

### Squid

Strengths:

- mature forward-proxy cache;
- dynamic TLS interception and certificate generation;
- native ACL and helper protocols;
- measured Bearer and Git Basic substitution semantics.

Open requirements:

- same-resolution address pinning;
- verified hard CONNECT lifetime through `client_lifetime` or composition;
- dynamic Console credential redemption without sending bodies to Console;
- verified no-cache helper semantics.

### mitmproxy

Strengths:

- Python addon API;
- HTTP/2 support through intercepted connections;
- direct request-header transformation;
- easier ordinary JSON integration than implementing an ICAP server.

Risks:

- addon exceptions fail open unless a later independent hook denies every flow
  not affirmatively marked;
- streaming must be enabled before any body read;
- `Flow.kill()` documents that flows already in transit cannot be killed
  reliably;
- no mature shared HTTP cache.

Mitmproxy is viable only with a fail-closed backstop and bounded connection
lifetime tested independently from the policy addon.

### Envoy

Envoy provides mature `ext_authz`, `max_stream_duration` and
`max_connection_duration`. Those are useful existing primitives for admission
and bounded flows.

Envoy did not substitute credentials in the earlier forward-proxy spike because
HTTPS remained an opaque CONNECT tunnel; the reverse-proxy test proved the
credential filter itself worked. Dynamic forward-proxy CONNECT support is still
alpha and Envoy has no Squid-like dynamic certificate machinery. It is therefore
not a complete replacement by itself, though a composed design may reuse its
stream controls.

### Purpose-built broker

A small admission/CONNECT broker is the fallback after existing controls are
tested. It should own only the missing pieces — authorization, address pinning
and bounded tunnelling — and compose with a proven credential transformer where
possible. Building a complete TLS-intercepting proxy from scratch is out of
scope; adding response caching is separately deferred.

## Failure behavior

The system denies on:

- missing, invalid, expired or wrong-audience proxy credentials;
- Console timeout or unavailability;
- malformed or incomplete authorization responses;
- destination denial or missing grants;
- DNS resolution failure, oversized answers or prohibited addresses;
- mismatch between selected and connected address;
- credential placeholder mismatch or denied redemption;
- credential adapter error or unavailable transformation service;
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
5. Mint endpoint-scoped Agent proxy credentials and a dedicated resolver.
6. Implement `POST /api/internal/http/authorize` as a read-only facade.
7. Define Console-owned egress credential handles and credential-use grants.
8. Implement `POST /api/internal/http/credentials/redeem` with mandatory secret
   redaction.
9. Decide the proxy-pod Cilium ceiling for grantable public origins.
10. Spike Squid DNS pinning, helper no-cache behavior and `client_lifetime` with
    active CONNECT traffic.
11. Implement the smallest conforming adapter or composition.
12. Run end-to-end grant, credential-selection, failure and rebinding tests.
13. Roll out one experimental Agent before generalizing the deployment.
14. Define a versioned structured request-capability model and bind credential
    redemption to its decision IDs.
15. Expose one read-only Grocy or similarly conventional API directly through
    instructions and an opaque placeholder; add generated tools only if their
    ergonomics justify the additional surface.
16. Model broad Gmail list/get coverage for an Agent whose existing policy
    allows all Gmail reads, using Google Discovery operation IDs, least-scope
    OAuth tokens and explicit route/query constraints.
17. Route direct Kubernetes HTTP through the existing canonical
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
- Console timeout, invalid JSON and invalid proxy credential deny;
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
- a newly issued raw placeholder is delivered once, cannot be read back, rotates
  with bounded overlap and is looked up only by fingerprint;
- Bearer and Git-over-HTTPS Basic forms substitute correctly;
- denied redemption never forwards the placeholder;
- request bodies, placeholders and replacements are absent from logs, metrics
  and cache;
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
- the Agent cannot reach another Agent's proxy;
- a provisioned sandbox inherits its authenticated caller's fence;
- removing the proxy fails closed;
- the Agent cannot mutate the label selecting its fence.

## Evidence retained from proxy experiments

Only observations that constrain implementation are retained here. Detailed
chronology belongs in Git history and the linked pull requests.

### Squid 7.6

The in-cluster spike demonstrated:

- TLS bump with dynamic origin certificates;
- destination-scoped Bearer placeholder replacement;
- base64 Basic replacement for Git over HTTPS;
- no-placeholder and unrelated-credential pass-through;
- destination matching must apply to both stripping and adding the header;
- `cache deny has_auth` prevents storage of authenticated responses;
- Debian `squid-openssl` provides the required OpenSSL and ICAP build features;
- one cache/process per Agent avoids unsafe cross-Agent cache and credential
  sharing.

The placeholder must never be sufficient by itself. Substitution is always the
conjunction of authenticated Agent, recognized placeholder, allowed destination
and active credential policy.

### Squid ICAP REQMOD

The REQMOD spike demonstrated:

- decrypted requests can be modified or blocked;
- service disconnect with `bypass=0` fails closed;
- explicit `icap_io_timeout` is required, and Squid retries once so observed
  denial latency is approximately twice the configured timeout;
- Squid sent complete POST bodies despite the service advertising `Preview: 0`;
- REQMOD ran before Squid's static header replacement.

These results rule out sending raw ICAP requests directly to Console. A
proxy-local adapter may use ICAP while sending only bounded metadata to the
Console JSON endpoints.

### Measured mitmproxy behavior

The addon spike demonstrated:

- intercepted HTTP/2 requests support credential substitution;
- streaming remains incremental only when enabled before body access;
- an addon exception forwards the request by default;
- a later fail-closed backstop can deny flows not marked by the policy addon;
- callbacks may need thread-safe scheduling onto mitmproxy's event loop.

A mitmproxy adapter therefore needs two independent layers: policy/transformation
and a structural deny backstop.

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
- <../../../plans/personal_agents/credential_proxy_options.md>

## References

- <https://www.squid-cache.org/Doc/config/external_acl_type/>
- <https://www.squid-cache.org/Doc/config/client_lifetime/>
- <https://www.squid-cache.org/Doc/config/request_header_replace/>
- <https://docs.mitmproxy.org/stable/api/events.html>
- <https://github.com/mitmproxy/mitmproxy/blob/main/mitmproxy/flow.py>
- <https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_authz_filter>
- <https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/core/v3/protocol.proto>
- <https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/route/v3/route_components.proto>
- <https://developers.google.com/workspace/gmail/api/reference/rest>
- <https://kubernetes.io/docs/reference/access-authn-authz/authorization/>
- <https://agentgateway.dev/blog/2026-07-27-credential-injection-ai-agent-egress-cb4a/>
- <https://infisical.com/blog/agent-proxy>
- <https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy>
