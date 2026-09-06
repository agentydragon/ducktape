# Agentplane workload authentication and credential propagation

Status: **authoritative design gate; accept and implement before Action Service implementation lands**.

This note fixes one boundary: how a managed Sandbox reaches Agentplane egress, LLM Proxy, Action
Service, and Kubernetes-native services without giving the runner or agent a real credential or
letting an intermediary replace the workload's identity with its own. It does not define a broad
Agent privilege framework, a new identity registry, or the ActionRequest lifecycle.

PR #5658 is superseded. PR #5657 is incomplete and superseded. PR #5662 is paused and
reference-only: its standalone Action Service domain/service work and direct destination
`TokenReview` seam may be salvageable, but its static subject-to-role mappings and credential
transport must be replaced by this design. No existing Action Service implementation is merge-ready.
In particular, direct Action Service `TokenReview` is the correct destination-authentication seam;
it was not inherently the wrong topology.

## Decision

Keep two bearer credentials with different consumers and authorities:

- `Proxy-Authorization: Bearer <agentplane-egress>` is hop authentication. The central egress proxy
  consumes and validates it, then applies its own EgressPolicy/EgressBinding and target policy.
- An end-to-end destination field such as `Authorization: Bearer <destination KSA JWT>` is
  destination authentication. LLM Proxy, Action Service, or the Kubernetes API consumes and
  validates it. The central proxy may carry, observe, and forward or inject this request data, but it
  does not semantically validate or authorize the destination credential merely because it passes
  through the proxy.

For native KSA service authentication, project a destination-audience token for the Sandbox Pod's
actual Kubernetes ServiceAccount into the sidecar only. Prefer sidecar-local transport or injection.
The destination derives the caller from that token and remains authoritative for audience, issuer,
Pod/ServiceAccount claims, liveness, service policy, and RBAC.

```text
runner / agent
    | ordinary HTTP(S) proxy traffic; inert placeholder where injection is required
    v
per-Sandbox egress-sidecar on loopback
    | Proxy-Authorization: Bearer <agentplane-egress>       (hop credential)
    | Authorization: Bearer <destination KSA JWT>           (end-to-end credential,
    |                                                        sidecar-supplied when configured)
    v
central egress proxy
    | consumes and validates Proxy-Authorization
    | rejects runner bypass; enforces host/method/path and EgressPolicy/EgressBinding
    | routes request; forwards or narrowly injects destination auth without validating it
    | never logs or persists either bearer
    +--------------------+----------------------+----------------------+
    v                    v                      v
LLM Proxy          Action Service       Kubernetes API        other upstream
TokenReview +      TokenReview +        native token          provider/service
WorkloadPrincipal WorkloadPrincipal    validation + RBAC     credential validation
```

The central proxy is an authenticated route, egress policy enforcement point, and—where explicitly
configured—credential substitution point. It is not the destination caller identity, a token mint,
a TokenRequest service, a signing authority, a destination-token validation oracle, or a generic
impersonation service. A proxy-derived identity header or the authenticated identity of the central
proxy must not become caller proof.

## Four separate decisions

Do not collapse these into one “auth token”:

1. **Hop authentication** proves which live Kubernetes workload's sidecar reached the central proxy.
   The sidecar sends `Proxy-Authorization: Bearer <agentplane-egress>`. The central proxy consumes
   it, validates it with `TokenReview`, verifies the configured Pod-bound claims, correlates the live
   Pod/Sandbox and source, and never forwards it.
2. **Destination authentication** proves a workload directly to the selected destination. For native
   KSA service auth, trusted Pod configuration projects a second token for the Pod's actual
   ServiceAccount and one fixed destination audience; the sidecar alone can read it. The destination
   receives it in its normal auth field, validates it for its own audience and issuer, resolves the
   workload, and applies service authorization or native RBAC. Central routing does not make the
   destination credential valid.
3. **Workload attribution** is destination-side for services that use KSA identity. Action Service
   and LLM Proxy resolve a validated Kubernetes subject and Pod UID through live Kubernetes and
   Agentplane state into the same `WorkloadPrincipal`: namespace, ServiceAccount, Pod, and Sandbox,
   plus optional authoritative Agent and Thread references. Missing Agent or Thread bindings remain
   absent.
4. **Authorization** remains split by authority. Central decides whether the authenticated sidecar
   may egress to the host/method/path under EgressPolicy/EgressBinding. Action Service owns
   ActionRequest policy; LLM Proxy owns model, routing, and quota policy; Kubernetes API applies
   native RBAC. Passing either layer does not grant the other layer's operation.

## Credential ownership and transport

### Central-held substitutions

Existing provider and service secrets continue to work as they do today. The runner presents the
configured inert placeholder; central evaluates the live workload, EgressPolicy/EgressBinding,
request target, credential reference, and exact substitution field, then substitutes the configured
Secret value.

Credential target and presentation policy are central-owned in this case because central owns the
Secret and performs the substitution. Central still does not need to validate the resulting API key,
provider token, or service credential. It substitutes the exact configured value; the provider or
service decides whether that credential is valid and authorized. Central must not return, persist,
or log the value.

### Native KSA destination authentication

At Sandbox Pod creation, trusted configuration may declare destination-audience projected
ServiceAccount token volumes. Each projection fixes:

- the Pod's actual ServiceAccount;
- one destination audience and expiry;
- one sidecar-only token path; and
- a trusted route or adapter that identifies the destination and presentation field.

The runner container cannot mount or read these projections. The agent cannot choose a
ServiceAccount, audience, token path, or destination credential. The central proxy receives no
TokenRequest or ServiceAccount impersonation authority. Never send every projected token to central
and never expose a token through an agent-visible response, file, environment variable, or log.

Prefer the sidecar to place the projected destination token into the destination request and send
that request through central as an authenticated egress route. The destination token is end-to-end
authentication even if the TLS-intercepting central proxy can see it as request data. Central forwards
it without TokenReview, destination authorization, or identity-header translation and does not log
or persist it.

The current generic sidecar sees a CONNECT host and port but relays opaque HTTPS after the tunnel is
established. If it cannot set an inner `Authorization` or other destination auth field, the real
implementation choices are:

1. a local TLS-terminating or service-specific adapter that can construct the destination request,
   inject the sidecar-only credential, and route it through central; or
2. a narrowly specified encrypted sideband on the authenticated sidecar-to-central hop that supplies
   one end-to-end credential for forwarding or exact-field injection after central intercepts TLS.

The sideband is credential transport, not delegated destination authentication. It must bind the
credential to trusted sidecar configuration and the selected CONNECT target, carry no caller-chosen
audience or token path, and expose no token to the runner. P0 may configure at most one destination
credential per unique CONNECT `host:port`; shared endpoints that need multiple credentials require a
separate local route/listener or service-specific adapter rather than inference from an opaque inner
path.

## Request flow and enforcement

For each request or tunnel:

1. The runner connects only to the loopback sidecar. Network policy and central admission reject a
   direct runner-to-central or runner-to-destination bypass.
2. The sidecar sends `Proxy-Authorization: Bearer <agentplane-egress>` to central. The runner cannot
   supply or override this header.
3. Central validates that hop token for `agentplane-egress`, correlates the live Pod/Sandbox and
   source, and enforces the requested CONNECT host plus applicable inner host/method/path and
   EgressPolicy/EgressBinding rules.
4. For a central-held Secret, central verifies the configured placeholder and performs the existing
   exact-field substitution. For native KSA auth, the sidecar either injects the end-to-end token
   locally or supplies the one configured token through the narrow sideband for forwarding/injection.
5. Central strips its consumed `Proxy-Authorization`, forwards the destination auth field, and does
   not TokenReview, authorize, log, or persist the destination credential.
6. The destination authenticates and authorizes the destination credential. Action Service and LLM
   Proxy use their shared `WorkloadPrincipal` authenticator/resolver; Kubernetes API uses native
   token validation and RBAC; external providers use their own credential semantics.

Central fails closed for missing or invalid hop auth, stale or unbound hop identity, direct bypass,
unknown or disallowed targets, EgressPolicy/EgressBinding denial, ambiguous sidecar credential
selection, credential/CONNECT binding mismatch, unavailable configured credential, or an unexpected
placeholder at a central-owned substitution point. A destination rejects wrong audience, issuer,
claims, liveness, service policy, or RBAC. Those destination failures are not reimplemented at
central.

## Shared `WorkloadPrincipal`

Action Service and LLM Proxy use the same destination authenticator and Agentplane resolver rather
than service-local subject maps:

```text
Authorization: Bearer <destination-audience KSA token>
  -> destination TokenReview
  -> validated namespace + ServiceAccount + Pod name/UID
  -> live Pod and owning Sandbox lookup
  -> optional live authoritative Thread/Agent binding lookup
  -> WorkloadPrincipal
```

The shared contract contains the destination-proven Kubernetes workload and Sandbox identity. Agent
and Thread appear only when the existing Agentplane control plane has a current authoritative
binding. A request body, Sandbox name, native provider thread ID, placeholder, caller-supplied
header, proxy identity, or proxy-derived identity header is not evidence for those identities.
Missing Agent or Thread remains absent; P0 does not invent a durable Agent registry to fill the gap.

Static `system:serviceaccount:*` to caller/operator role lists are insufficient. They authenticate a
coarse subject but do not establish the live Pod/Sandbox or optional Agent/Thread that owns the
request. Service policy evaluates the resolved `WorkloadPrincipal` and current service-owned policy.
Operator or end-user access is a separate authenticated BFF/operator path, not a role inferred from
a Sandbox destination token.

Central may pass non-authoritative correlation IDs and egress policy/rule/credential decision
references. Destinations may log them as routing or audit context, but they must not accept them as
caller proof or as a substitute for validating the destination credential.

## Destination behavior

### Action Service and LLM Proxy

Both are independently deployed internal services reached through the sidecar and central route.
Each:

- receives a token projected for its configured audience and the Pod's actual ServiceAccount;
- validates audience, issuer, and Kubernetes workload claims directly through the shared
  `WorkloadPrincipal` authenticator/resolver;
- rejects a stale or deleted Pod, changed owner, unresolved Sandbox, or unauthorized operation;
- leaves Agent/Thread absent when no authoritative current binding exists; and
- applies its own authorization to the requested operation.

Action Service must not accept `agent_id`, `sandbox_id`, `thread_id`, owner, caller role, central
identity, or proxy-derived headers as proof. LLM Proxy follows the same rule for model requests and
provider-native thread metadata. Central can require that all Sandbox traffic use the governed route,
but the destination does not need central to prove or pre-validate the destination token.

### Kubernetes API

For a Kubernetes API destination, the projected token uses the Kubernetes API's configured audience.
The API server validates the token and applies native RBAC RoleBindings to the Pod's ServiceAccount.
Central enforces its egress route policy and forwards the request; it does not translate the
principal, TokenReview the destination token, or make the RBAC decision.

Ordinary non-apiserver services do **not** automatically inherit Kubernetes RoleBindings merely
because they validate a KSA token. Action Service and LLM Proxy apply explicit service policy over
the resolved `WorkloadPrincipal` unless they deliberately integrate with Kubernetes authorization,
for example through `SubjectAccessReview`. Audience validation is authentication and isolation, not
authorization by itself.

### Other upstreams

External providers may continue to use central-held operator-configured secrets or other destination
credentials. Central owns the target, policy, placeholder, and field binding for substitutions it
performs, but the upstream remains authoritative for the substituted credential's validity and
permissions.

## Scope and acceptance gate

**P0 behavior**

- keep `Proxy-Authorization: Bearer <agentplane-egress>` as sidecar-to-central hop auth only; central
  consumes and validates it and never forwards it;
- keep destination authentication in the destination's normal end-to-end field, such as
  `Authorization: Bearer <destination KSA JWT>`;
- enforce direct-bypass rejection and central host/method/path plus EgressPolicy/EgressBinding policy
  independently from destination authentication and authorization;
- preserve existing central-held Secret placeholder substitution without requiring central to
  validate the substituted provider or service credential;
- keep projected destination KSA tokens sidecar-only and agent-invisible; implement either local
  injection/transport or the narrow one-credential-per-CONNECT-`host:port` sideband required by the
  generic opaque CONNECT seam;
- make central forward or inject a configured destination token without acting as TokenRequest,
  TokenReview, authorization, or proxy-derived identity authority for it;
- make Action Service and LLM Proxy validate destination tokens directly through the same
  `WorkloadPrincipal` authenticator/resolver and apply service-owned policy; Kubernetes API uses
  native validation and RBAC; and
- prove end to end that the runner sees placeholders only, cannot bypass the sidecar/central route,
  cannot forge identity metadata, and cannot select another destination credential; central rejects
  invalid hop auth and disallowed egress, while destinations reject invalid or unauthorized
  destination credentials and both internal services resolve the same live workload identity.

**Observed evidence preserved**

- the existing sidecar, central proxy, hop `TokenReview`, live Pod/Sandbox correlation,
  EgressPolicy/EgressBinding evaluation, TLS interception, and exact placeholder substitution are
  the starting topology;
- the accepted ActionRequest/Decision/Execution behavior remains unchanged; and
- #5662 demonstrates potentially reusable standalone Action Service domain/service work and the
  correct direct-`TokenReview` destination seam, but not an acceptable mapping or transport yet.

**Deferred**

- optional central comparison of hop and destination token provenance as defense in depth; it is not
  core authentication correctness and must not make central a destination-token oracle;
- multiple credentials or audiences on one CONNECT `host:port`, pending dedicated local routes or a
  service adapter;
- authoritative Agent/Thread attribution until a live control-plane binding exists;
- optional `SubjectAccessReview` or other explicit RBAC integration for non-apiserver services;
- external-agent authentication, standing grants, cross-agent data policy, and a general capability
  framework; and
- a credential broker, dynamic TokenRequest service, identity registry, or generic impersonation
  layer.

Acceptance of this document fixes the implementation contract. The independently deployable
standalone Action Service slice remains blocked until the destination-credential transport seam,
shared authenticator, and end-to-end evidence are implemented and reconciled. Nothing in #5657,
#5658, or #5662 is merge-ready as-is.
