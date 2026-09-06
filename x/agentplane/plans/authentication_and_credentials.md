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

Use the same caller-authentication mechanism for Action Service, LLM Proxy, and future
Kubernetes-native access: a sidecar-held, destination-audience projected token for the Sandbox Pod's
actual Kubernetes ServiceAccount. The central proxy transports and substitutes that token, but the
destination validates it directly and derives the caller from it.

```text
runner / agent
    | ordinary HTTP(S) proxy traffic; inert placeholder only
    v
per-Sandbox egress-sidecar on loopback
    | hop token: projected Pod KSA token, aud=agentplane-egress
    | destination token: separate sidecar-only projected Pod KSA token,
    |                    audience fixed by trusted Pod configuration
    v
central egress proxy
    | validates both tokens with TokenReview for their configured audiences
    | requires the same Pod UID + ServiceAccount on both proofs
    | applies live Pod/Sandbox lookup and exact policy/binding/target checks
    | TLS interception; substitutes destination token only in exact auth field
    +--------------------+----------------------+----------------------+
    v                    v                      v
LLM Proxy          Action Service       Kubernetes API        other upstream
TokenReview +      TokenReview +        native token          configured
WorkloadPrincipal WorkloadPrincipal    validation + RBAC     destination auth
```

The `agentplane-egress` token remains a hop credential from sidecar to central only. It is never
forwarded and authorizes no destination operation. The destination token is a separate projection,
held only by the sidecar, for the Pod's real ServiceAccount and one operator-configured audience.
The agent emits only an inert placeholder.

The central proxy is a bounded credential relay and policy enforcement point. It is not the caller
identity, a token mint, a signing authority, or a generic impersonation service. A proxy-derived
private context header or the authenticated identity of the central proxy must not become the
primary proof of the caller. The central proxy may attach non-authoritative decision references or
correlation metadata, but every destination that uses workload identity validates the substituted
KSA token itself.

## Four separate decisions

Do not collapse these into one “auth token”:

1. **Hop authentication** proves which live Kubernetes workload reached the central proxy. The
   sidecar presents its projected `agentplane-egress` token over the authenticated relay. The
   central proxy validates it with `TokenReview`, verifies Pod-bound claims, and correlates the live
   Pod UID, ServiceAccount, source, and owning Sandbox. This token goes no farther.
2. **Destination authentication** proves the same workload directly to the selected destination.
   The trusted Pod template/controller projects a second token for the Pod's actual ServiceAccount
   and a fixed destination audience. The sidecar alone can read it. The central proxy validates and
   relays that exact token, then substitutes it only into the configured intercepted auth field.
   Action Service, LLM Proxy, or the Kubernetes API validates the substituted token for its own
   audience.
3. **Workload attribution** resolves a validated Kubernetes subject and Pod UID through live
   Kubernetes and Agentplane state into a shared `WorkloadPrincipal`: namespace, ServiceAccount,
   Pod, and Sandbox, plus optional authoritative Agent and Thread references. Missing Agent or
   Thread bindings remain absent.
4. **Authorization** remains the destination's decision. Action Service owns ActionRequest policy;
   LLM Proxy owns model, routing, and quota policy; Kubernetes API applies native RBAC. Egress
   admission and successful authentication do not themselves allow an operation.

## Token projection and bounded relay

At Sandbox Pod creation, trusted configuration declares a finite set of projected ServiceAccount
token volumes. Each destination projection fixes:

- the Pod's actual ServiceAccount;
- one audience and expiry;
- one sidecar-only token path;
- one inert placeholder;
- one exact destination host, port, protocol, and intercepted auth field; and
- the EgressPolicy/EgressBinding credential definition allowed to select it.

The runner container cannot mount or read these projections. The agent cannot choose a
ServiceAccount, audience, token path, destination, or presentation field. Adding a destination token
requires a trusted Pod-template/controller and binding change before the Pod starts, not a runtime
request from the agent or central proxy.

For each request or tunnel:

1. The agent sends the configured inert placeholder in the ordinary destination auth field.
2. The sidecar selects at most one preconfigured destination credential from trusted local routing
   configuration and reads that projection just in time.
3. The sidecar sends the hop token, inert selector, and at most that one destination token to the
   central proxy over an encrypted, authenticated relay. A plaintext bearer header on the existing
   sidecar-to-central connection is not acceptable.
4. The central proxy validates the hop token with `TokenReview` for `agentplane-egress` and the
   destination token with `TokenReview` for the credential's configured audience.
5. Both reviews must identify the same Pod UID and ServiceAccount. The central proxy also requires
   the live Pod, owning Sandbox, source, CONNECT target, placeholder, EgressPolicy/EgressBinding,
   credential slot, audience, and presentation target to agree.
6. After TLS interception, the central proxy verifies the inner request still contains the expected
   placeholder in the exact configured auth field. It replaces only that field with the validated
   destination token. It never returns, persists, or logs either bearer.

Any missing or stale Pod, wrong audience, ServiceAccount or Pod mismatch, unknown placeholder,
ambiguous selection, binding mismatch, target mismatch, unavailable token, or unexpected inner auth
value fails closed. The central proxy has no TokenRequest or ServiceAccount impersonation authority.
Never send all projected tokens to the central proxy.

## P0 HTTPS CONNECT selector

The sidecar sees the CONNECT host and port before it relays opaque TLS; the central proxy sees the
intercepted inner request and placeholder. P0 therefore allows exactly one configured destination
credential for each unique CONNECT `host:port`.

The sidecar selects that one credential from trusted configuration, never from a caller-supplied
audience or token name. The central proxy binds it to the outer CONNECT target and then checks the
inner placeholder, destination, and auth field after interception before substitution. HTTP/2
connection coalescing must not widen the configured target.

Multiple credentials or audiences on one CONNECT `host:port` are deferred. Supporting them requires
one dedicated local listener/route per credential or a service-specific adapter that gives the
sidecar an unambiguous trusted selection before the tunnel is opened. Do not guess from an inner
path the sidecar cannot see, request a token after the tunnel begins, or send every candidate token.

## Shared `WorkloadPrincipal`

Action Service and LLM Proxy use the same destination authenticator and Agentplane resolver rather
than service-local subject maps:

```text
substituted destination-audience KSA token
  -> destination TokenReview
  -> validated namespace + ServiceAccount + Pod name/UID
  -> live Pod and owning Sandbox lookup
  -> optional live authoritative Thread/Agent binding lookup
  -> WorkloadPrincipal
```

The shared contract contains the proven Kubernetes workload and Sandbox identity. Agent and Thread
appear only when the existing Agentplane control plane has a current authoritative binding. A
request body, Sandbox name, native provider thread ID, placeholder, or caller-supplied header is not
evidence for those identities. Missing Agent or Thread remains absent; P0 does not invent a durable
Agent registry to fill the gap.

Static `system:serviceaccount:*` to caller/operator role lists are insufficient. They authenticate a
coarse subject but do not establish the live Pod/Sandbox or optional Agent/Thread that owns the
request. Service policy evaluates the resolved `WorkloadPrincipal` and current service-owned policy.
Operator or end-user access is a separate authenticated BFF/operator path, not a role inferred from
a Sandbox destination token.

The central proxy may pass non-authoritative correlation IDs, selected policy/rule/credential
references, and egress decision metadata on its protected hop. Destinations may log or compare those
values, but identity headers and the central proxy's own mTLS identity are not caller proof and must
not replace direct token validation.

## Destination behavior

### Action Service and LLM Proxy

Both are independently deployed internal services reached through the existing sidecar and central
proxy. Each:

- receives a token projected for its configured audience and the Pod's actual ServiceAccount;
- validates that token directly through the shared `WorkloadPrincipal` authenticator/resolver;
- rejects a wrong audience, stale or deleted Pod, changed owner, or unresolved Sandbox;
- leaves Agent/Thread absent when no authoritative current binding exists; and
- applies its own authorization to the requested operation.

Action Service must not accept `agent_id`, `sandbox_id`, `thread_id`, owner, caller role, or central
identity headers as proof. LLM Proxy follows the same rule for model requests and provider-native
thread metadata. A direct Sandbox-to-service path that bypasses egress policy remains disallowed,
but the destination does not delegate caller authentication to the central proxy.

### Kubernetes API

For a Kubernetes API destination, the projected token uses the Kubernetes API's configured audience.
The API server validates the token and applies native RBAC RoleBindings to the Pod's ServiceAccount.
The central proxy performs the same relay checks but does not translate the principal or make the
RBAC decision.

Ordinary non-apiserver services do **not** automatically inherit Kubernetes RoleBindings merely
because they validate a KSA token. Action Service and LLM Proxy apply explicit service policy over
the resolved `WorkloadPrincipal` unless they deliberately integrate with Kubernetes authorization,
for example through `SubjectAccessReview` or another explicit RBAC-backed check. Audience validation
is authentication and isolation, not authorization by itself.

### Other upstreams

External providers may continue to use operator-configured secrets or other destination credentials.
They use the same exact placeholder/binding/target discipline, but they need not implement
`WorkloadPrincipal` when they do not understand Kubernetes identity.

## Scope and acceptance gate

**P0 behavior**

- retain the `aud=agentplane-egress` hop token for sidecar-to-central authentication only;
- add sidecar-only destination projections whose audiences and credential bindings are fixed by
  trusted Pod configuration for the Pod's actual ServiceAccount;
- carry at most one selected destination token over an encrypted/authenticated relay;
- implement the one-credential-per-CONNECT-`host:port` selector and exact inner-placeholder check;
- require central `TokenReview` of both tokens and equality of Pod UID and ServiceAccount before
  substitution into the exact intercepted auth field;
- make Action Service and LLM Proxy directly validate their substituted token through the same
  `WorkloadPrincipal` authenticator/resolver and apply service-owned policy; and
- prove end to end that the runner sees placeholders only, forged identity metadata is ignored,
  direct/bypass calls fail, mismatched hop/destination identities fail, wrong audiences and targets
  fail, and both services resolve the same live workload identity.

**Observed evidence preserved**

- the existing sidecar, central proxy, hop `TokenReview`, live Pod/Sandbox correlation,
  EgressPolicy/EgressBinding evaluation, TLS interception, and exact placeholder substitution are
  the starting topology;
- the accepted ActionRequest/Decision/Execution behavior remains unchanged; and
- #5662 demonstrates potentially reusable standalone Action Service domain/service work and the
  correct direct-`TokenReview` destination seam, but not an acceptable mapping or transport yet.

**Deferred**

- multiple credentials or audiences on one CONNECT `host:port`, pending dedicated local routes or a
  service adapter;
- authoritative Agent/Thread attribution until a live control-plane binding exists;
- optional `SubjectAccessReview` or other explicit RBAC integration for non-apiserver services;
- external-agent authentication, standing grants, cross-agent data policy, and a general capability
  framework; and
- a credential broker, dynamic TokenRequest service, identity registry, or generic impersonation
  layer.

Acceptance of this document fixes the implementation contract. The independently deployable
standalone Action Service slice remains blocked until this auth transport, shared authenticator, and
end-to-end evidence are implemented and reconciled. Nothing in #5657, #5658, or #5662 is merge-ready
as-is.
