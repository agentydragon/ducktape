# Agentplane workload authentication and credential propagation

Status: **authoritative P0 design gate; implement and prove before standalone Action Service lands**.

This note fixes one boundary: how a managed Sandbox reaches Agentplane egress, LLM Proxy, Action
Service, and Kubernetes-native services while the harness/runner never possesses a real credential.
It does not define a broad Agent privilege framework, a new identity registry, or the ActionRequest
lifecycle.

PR #5658 is superseded. PR #5657 is incomplete and superseded. PR #5662 is paused and
reference-only: its standalone Action Service domain/service work may be salvageable, but its auth
must use the shared `WorkloadPrincipal` and the central dynamic credential source defined here. No
existing standalone Action Service implementation is merge-ready.

## P0 decision

Use one sidecar-only, projected, Pod-bound Kubernetes ServiceAccount workload token with one shared
first-party audience. Prefer the audience name `agentplane-workload`. Existing deployments may
transition from the current `agentplane-egress` audience/name rather than changing it atomically; the
required property is one configured shared workload audience, not the literal spelling.

The token has two uses along one authenticated path, not two independent credentials:

1. The Pod-local sidecar reads the projection and adds it as
   `Proxy-Authorization: Bearer <workload token>` on the hop to central. The harness/runner cannot
   mount, read, supply, or override it.
2. Central validates that token once for its own ingress/hop authentication, retains the successful
   authenticated token object/value as request or tunnel context, and resolves the live
   Pod-to-Sandbox binding as it does today.
3. If an allowed request presents the placeholder for an `EgressCredential` whose source is
   `authenticatedWorkloadToken`, central substitutes that same already-authenticated bearer into the
   exact configured target. There is no second destination token for central to validate.
4. LLM Proxy, Action Service, or another participating destination directly validates the bearer it
   receives and resolves the workload. Central's validation proves admission to central; it does not
   replace destination authentication or authorization.

```text
harness / runner
    | ordinary HTTP(S) proxy traffic
    | Authorization: Bearer agentplane-credential-agentplane-workload
    | (inert derived placeholder; no real token)
    v
per-Sandbox egress sidecar on loopback
    | adds Proxy-Authorization: Bearer <projected Pod-bound workload token>
    | harness cannot read or override the projection
    v
central egress proxy
    | validates Proxy-Authorization once for central ingress
    | correlates source + live Pod -> Sandbox
    | associates the authenticated bearer with this request / CONNECT tunnel
    | EgressPolicy selects exact host/method/path and credentialRef
    | EgressCredential target selects exact placeholder presentation
    | substitutes the validated bearer for that placeholder; strips Proxy-Authorization
    +--------------------------+--------------------------+
    v                          v                          v
LLM Proxy                 Action Service          participating K8s service
TokenReview + shared      TokenReview + shared    TokenReview/native validation
WorkloadPrincipal         WorkloadPrincipal       plus service authorization
```

The central proxy and Pod-local proxy remain generic. Neither contains an LLM Proxy or Action
Service case. Destination selection, allowed host/method/path, credential selection, and header
presentation come entirely from `EgressPolicy` and `EgressCredential` configuration.

## One authenticated bearer, separate authorities

Do not confuse reuse of the token value with collapse of the policy boundaries:

1. **Central hop authentication.** Central accepts the sidecar only after validating the configured
   workload audience, issuer, Pod-bound claims, source-Pod correlation, and live Pod/Sandbox state.
   It strips the consumed `Proxy-Authorization` before upstream forwarding.
2. **Dynamic credential resolution.** `authenticatedWorkloadToken` resolves to the authenticated
   token object/value retained after successful central validation. It must not reread or blindly
   copy an arbitrary raw `Proxy-Authorization` header. If authenticated request/tunnel context is
   absent, stale, mismatched, or unbound, resolution fails closed.
3. **Destination authentication.** A selected destination receives the bearer only through normal
   exact placeholder substitution and validates it directly. It does not trust central identity
   headers, proxy identity, Sandbox IDs, or caller-supplied identity metadata.
4. **Authorization.** Central decides whether this Sandbox may use this route and credential at the
   configured host/method/path. Each destination independently decides whether the resolved
   `WorkloadPrincipal` may perform the requested operation.

Central is an authenticated route, policy enforcement point, and configured credential substitution
point. It is not a token mint, TokenRequest service, signing authority, destination authorization
service, identity-header authority, or generic impersonation service. This design creates no Secret
per Sandbox and grants central no TokenRequest authority.

## Generic `EgressCredential` source

Extend `EgressCredential.spec.source` from its current single shape into a source union:

- `secretRef` remains the existing static central-held Secret source; and
- `authenticatedWorkloadToken` is a dynamic, per-authenticated-request source whose value is the
  bearer retained from successful central hop validation.

The placeholder remains derived from the `EgressCredential` name. Existing target methods remain the
presentation authority. For the workload bearer, prefer `schemeToken` with `Bearer` so a request must
present exactly `Authorization: Bearer agentplane-credential-agentplane-workload`; do not append or
overwrite `Authorization` unconditionally. Exact substitution preserves explicit credential
selection, current target semantics, and fail-closed behavior for wrong headers, schemes, targets,
or unbound placeholders.

Illustrative configuration (schema spelling may be refined during implementation, but the source
union and semantics are fixed):

```yaml
apiVersion: agentplane.ducktape.ai/v1alpha1
kind: EgressCredential
metadata:
  name: provider-api-key
  namespace: sandboxes
spec:
  description: Provider API key held centrally
  source:
    secretRef:
      name: provider-credentials
      key: api-key
  targets:
    - header: Authorization
      method: schemeToken
      scheme: Bearer
---
apiVersion: agentplane.ducktape.ai/v1alpha1
kind: EgressCredential
metadata:
  name: agentplane-workload
  namespace: sandboxes
spec:
  description: Calling Sandbox Pod workload identity for trusted first-party services
  source:
    authenticatedWorkloadToken: {}
  targets:
    - header: Authorization
      method: schemeToken
      scheme: Bearer
---
apiVersion: agentplane.ducktape.ai/v1alpha1
kind: EgressPolicy
metadata:
  name: first-party-services
  namespace: sandboxes
spec:
  rules:
    - hosts: [llm-proxy.agentplane.svc.cluster.local]
      methods: [POST]
      paths: [/v1/chat/completions, /v1/responses]
      clusterInternal: true
      credentialRef:
        name: agentplane-workload
    - hosts: [action-service.agentplane.svc.cluster.local]
      methods: [POST]
      paths: [/v1/action-requests, /v1/action-requests/*/decisions]
      clusterInternal: true
      credentialRef:
        name: agentplane-workload
```

For either rule, the harness sends the derived placeholder in the declared target. Policy matching
selects the rule and `credentialRef`; the target parse proves the placeholder was presented exactly;
only then does central resolve the source. `secretRef` resolves from configured Secret state.
`authenticatedWorkloadToken` resolves differently for every authenticated request/tunnel and never
materializes as a Kubernetes Secret or a Sandbox-owned object.

## HTTPS CONNECT requires no new sideband

The current central proxy already authenticates the sidecar's `Proxy-Authorization` and associates
that admission with the CONNECT tunnel. After TLS interception, central sees the inner HTTP header
that carries the credential placeholder. It can therefore apply the ordinary host/method/path policy,
resolve that tunnel's already-authenticated workload bearer, and use the ordinary target substitution
logic.

P0 does **not** add:

- a second projected destination token;
- sidecar-local destination-header injection;
- an encrypted credential sideband;
- one-token-per-CONNECT selection or binding machinery;
- LLM Proxy or Action Service branches in either proxy; or
- central TokenRequest authority.

The authenticated tunnel context is the only context the dynamic source needs. Re-authentication or
loss of that context fails closed rather than falling back to a raw header.

## Destination authentication and `WorkloadPrincipal`

LLM Proxy and Action Service accept the shared workload audience and use one shared authenticator and
live resolver:

```text
Authorization: Bearer <agentplane-workload token>
  -> direct TokenReview for the shared audience
  -> validated namespace + ServiceAccount + Pod name/UID
  -> live Pod and owning Sandbox lookup
  -> optional live authoritative Thread/Agent binding lookup
  -> WorkloadPrincipal
```

`WorkloadPrincipal` contains namespace, ServiceAccount, Pod name/UID, and Sandbox, plus Agent and
Thread only when current authoritative Agentplane bindings exist. Missing Agent or Thread remains
absent. A request body, Sandbox name, provider-native thread ID, placeholder, central identity,
caller-supplied header, or proxy-derived identity header is not identity evidence.

Both destinations reject invalid audience/issuer, stale or deleted Pods, changed ownership,
unresolved Sandboxes, and unauthorized operations. LLM Proxy owns model/routing/quota policy. Action
Service owns ActionRequest policy. Kubernetes-native services that intentionally accept the same
audience can use the same authenticator/resolver, but token authentication never supplies their
service authorization automatically.

## Shared-audience tradeoff and P0 containment

A shared audience is intentionally the smallest P0 mechanism, but it weakens recipient isolation:
any recipient that sees the bearer could replay it to another recipient that accepts the same
audience.

Contain that risk structurally in P0:

- only trusted first-party recipients accept the shared audience;
- NetworkPolicy and listener restrictions make those recipients reachable from the central proxy
  path, not directly from Sandbox Pods and, where feasible, not from one another;
- central correlates the validated token's Pod claims with the source Pod/tunnel, preventing a token
  observed elsewhere from being replayed back through central as another source;
- projected tokens are short-lived and Pod-bound;
- every recipient performs direct token validation, live workload resolution, and service-owned
  authorization; and
- credentials, placeholders where sensitive, and auth headers do not appear in logs, errors,
  decisions, traces, or responses.

Acceptance should verify the NetworkPolicy/listener topology and direct-bypass rejection where the
test environment permits. It cannot prove a malicious trusted recipient will never exfiltrate a
bearer; that is the explicit residual trust in this P0 choice.

If recipient isolation becomes required, add per-destination projected audiences later through the
same credential-source abstraction. That may require a future generic sidecar credential source or
transport extension. Do not build that machinery before a concrete isolation requirement or failed
containment test demands it.

## Kubernetes API and native RBAC later

The Kubernetes API server must accept the shared workload audience for this same token to
authenticate there. If the API server does not or should not accept it, use a distinct projected
API-audience token through a future generic sidecar credential-source extension. Do not grant central
TokenRequest authority merely to bridge the audiences.

The API server enforces RoleBindings for Kubernetes API requests. Ordinary services do **not**
inherit Kubernetes RBAC merely because they validate a KSA token. They need explicit service policy
or an intentional authorization integration such as `SubjectAccessReview`.

## Request flow and failures

For each request or CONNECT tunnel:

1. The harness connects only to the loopback sidecar and carries only inert placeholders.
2. The sidecar adds its sidecar-only projected workload token as `Proxy-Authorization`.
3. Central validates the token once for its own ingress, correlates source Pod and live Sandbox, and
   associates the successful authenticated bearer with the request/tunnel.
4. Central evaluates CONNECT and inner host/method/path under EgressPolicy/EgressBinding.
5. If the selected rule names a credential, central requires an exact matching placeholder and
   target. For `authenticatedWorkloadToken`, it substitutes the authenticated tunnel/request bearer;
   for `secretRef`, it retains the existing Secret substitution behavior.
6. Central strips `Proxy-Authorization`, forwards the substituted request, and never logs or returns
   either credential value.
7. The destination directly authenticates the bearer, resolves `WorkloadPrincipal`, and applies its
   own authorization.

Fail closed for missing, forged, invalid, stale, or wrongly-audienced `Proxy-Authorization`; source
Pod mismatch; direct runner bypass; absent authenticated context; unknown or disallowed target;
EgressPolicy/EgressBinding denial; wrong host/method/path; missing, malformed, wrong-target, or
unbound placeholder; unavailable source; or any attempt to use the dynamic source outside the
validated request/tunnel. A destination independently rejects invalid identity, unresolved live
ownership, or unauthorized service operations.

## Acceptance gate

**P0 behavior**

Use two Sandbox Pods—covering the same ServiceAccount and distinct ServiceAccounts across the suite
if practical—to prove:

- central substitutes each request's own successfully validated Pod-bound token, never another Pod's
  token or a static shared value;
- LLM Proxy and Action Service independently TokenReview the received bearer and resolve the correct
  Sandbox through the same `WorkloadPrincipal` contract;
- the harness sees and can obtain only the derived placeholder, never the real token;
- forged `Proxy-Authorization`, direct central/destination bypass, wrong host/method/path/credential
  target, missing authenticated context, and an unbound or unresolved placeholder all fail closed;
- HTTPS CONNECT uses authenticated tunnel context and inner exact substitution without a sideband;
- same-ServiceAccount Pods remain distinguishable by Pod-bound identity and live Pod-to-Sandbox
  resolution;
- NetworkPolicy/listener restrictions and central source-Pod correlation exercise the feasible
  shared-audience replay-containment assumptions; and
- no real token appears in application, proxy, destination, audit, test, or error logs.

The strongest assertion is end to end: each destination reports the expected live Sandbox identity
for each Pod while the corresponding harness can demonstrate only its inert placeholder. Unit tests
of source resolution or bookkeeping alone are not acceptance.

**Observed evidence preserved**

- the existing sidecar, central `TokenReview`, source-Pod/live Sandbox correlation,
  EgressPolicy/EgressBinding evaluation, TLS interception, and exact placeholder substitution are the
  starting topology;
- existing `secretRef` credentials and targets keep their current semantics; and
- #5662 contains potentially reusable Action Service domain/service work, but its auth and transport
  assumptions are reference-only.

**Deferred**

- per-destination audiences and recipient-isolated projected tokens;
- a generic sidecar credential-source/transport extension if a destination requires a token central
  cannot derive from authenticated request context;
- Kubernetes API access with a distinct API audience when the shared audience is inappropriate;
- optional `SubjectAccessReview` integration for ordinary services;
- authoritative Agent/Thread attribution until live bindings exist; and
- external-agent authentication, standing grants, cross-agent data policy, a credential broker,
  identity registry, or general capability framework.

Acceptance of this document fixes the implementation contract. Standalone Action Service remains
blocked until the shared-token dynamic substitution path, shared `WorkloadPrincipal`, and end-to-end
acceptance evidence are implemented and proven. Nothing in #5657, #5658, or #5662 is merge-ready
as-is.
