# Agentplane workload authentication and credential propagation

Status: **authoritative design gate; accept before Action Service implementation**.

This note fixes one boundary: how a managed Sandbox reaches Agentplane egress, LLM Proxy, Action
Service, and future Kubernetes services without giving the runner or agent a real credential or
letting it assert its own identity. It does not define a broad Agent privilege framework, a new
identity registry, or the ActionRequest lifecycle.

PR #5657 is superseded by this design. PR #5658 is reference-only and was already structurally
superseded by #5662. PR #5662 is also reference-only: in particular, its direct Action Service
TokenReview and separate `agentplane-actions` relay token are not the accepted topology. Do not
modify or merge any of those PRs. No Action Service implementation PR is merge-ready until this
design is accepted and the implementation is reconciled to it.

## Decision

Keep the implemented network and trust topology:

```text
runner / agent
    | ordinary HTTP(S) proxy traffic; inert placeholders only
    v
per-Sandbox egress-sidecar on loopback
    | adds projected, short-lived Pod KSA token for audience agentplane-egress
    v
central egress proxy
    | TokenReview; live Pod -> Sandbox lookup; EgressPolicy/EgressBinding
    | TLS interception; exact placeholder substitution; context propagation
    +--------------------+---------------------+
    v                    v                     v
LLM Proxy          Action Service       allowlisted upstream
service auth       service auth         destination auth
```

The sidecar re-reads the projected `agentplane-egress` token for each request and places it only in
`Proxy-Authorization`. The central proxy remains the one ingress for managed-Sandbox service calls.
It validates that hop token, correlates its Pod UID and source with the live Pod and owning Sandbox,
and applies current `EgressPolicy` and `EgressBinding` state before forwarding anything. Existing
TLS interception and placeholder substitution remain the credential-delivery mechanism for HTTP and
intercepted gRPC metadata.

Action Service and LLM Proxy are therefore destinations behind the same authenticated egress path,
not new peers that each receive a token from the runner. They consume the same proxy-derived
workload context and placeholder/binding vocabulary, but each owns its own service authorization.
Where destination authentication is needed, use a credential scoped to that service and preferably
to that service's audience; do not reuse the `agentplane-egress` hop token as a destination bearer.

The central proxy may select and present configured credentials. It is **not** a generic TokenRequest,
impersonation, signing, or arbitrary token-minting authority. It must never accept a caller-chosen
ServiceAccount, audience, destination, or identity and turn that request into a bearer token.

## Four separate decisions

Do not collapse these into one “auth token”:

1. **Hop authentication** proves which live Kubernetes workload reached the central proxy. The
   sidecar's projected `agentplane-egress` token, TokenReview, Pod-bound claims, source correlation,
   and live Pod/Sandbox lookup provide this. It authorizes no Action and is not forwarded upstream.
2. **Workload attribution** is the trusted context derived from that proof: Kubernetes namespace,
   ServiceAccount, Pod name/UID, and Sandbox name/UID. Agent and Thread references may be added only
   when the existing control plane that created/attached them supplies an authoritative current
   binding. Missing bindings remain absent; the runner's headers, request body, native provider
   thread ID, and Sandbox name are never evidence for Agent or Thread identity.
3. **Destination authentication** is what the selected upstream accepts: for example a provider API
   key, a service-specific bearer, or a destination-audience KSA token. It is selected from an
   operator-authored binding, inserted only at its exact presentation target, never returned to the
   Sandbox, and never inferred from a caller-provided audience.
4. **Service authorization** is the destination's decision over the authenticated workload context
   and requested operation. LLM Proxy owns model/routing/quota policy. Action Service owns
   ActionRequest submission, read, decision, and execution policy. Egress admission proves neither.

## Shared workload context

After hop authentication, the central proxy constructs one internal workload-context envelope:

```text
workload: namespace, serviceAccount, podName, podUID, sandboxName, sandboxUID
agent:     optional authoritative product Agent reference
thread:    optional authoritative Agentplane Thread reference
binding:   EgressBinding / EgressPolicy / rule / credential decision references
```

The exact wire encoding is an implementation choice, but the trust rules are not:

- the proxy removes any incoming copies of context headers;
- only values derived from TokenReview/live lookup or an authoritative control-plane binding enter
  the envelope;
- the envelope travels only on the protected proxy-to-service hop, with peer authentication or an
  equivalent network boundary that prevents a Sandbox from calling the service and supplying it;
- a service rejects the internal context form on any public or Sandbox-reachable listener; and
- logs and durable ActionRequest records may retain identifiers and decision references, never hop
  or destination bearer values.

P0 does not require a durable Agent identity. Until an authoritative Agent/Thread binding exists,
services authorize and record the proven Sandbox workload and leave Agent/Thread absent. This is
preferable to inventing a registry or trusting self-asserted correlation fields. When the existing
Agentplane control plane owns such a binding, both LLM Proxy and Action Service must consume the same
resolver/envelope rather than maintaining service-local mappings.

## Placeholder, credential, binding, and target

Use one vocabulary for provider egress and Agentplane services:

- A **placeholder** is an inert, namespace-unique selector such as the existing
  `agentplane-credential-<name>`. Possessing or naming it is not authentication.
- A **credential definition** names exactly one source class and every allowed presentation. Current
  central `Secret` references remain valid sources. A future sidecar-projected KSA token is a
  distinct source class; it is not a Secret reference hidden behind the same implementation.
- A **target** fixes destination service/host, port and protocol plus the exact presentation point
  (for example `Authorization: Bearer`, a whole gRPC metadata value, or an internal service-auth
  field). For a KSA token it also fixes the audience and ServiceAccount source selected at Pod
  creation. Host/path policy and credential presentation must agree; a credential cannot be spent
  on another matching host merely because its placeholder was supplied.
- An **EgressPolicy** admits exact traffic and names the credential definition, if any.
- An **EgressBinding** grants named policies to the proven Sandbox subject. It never binds an
  Agent/Thread asserted by the request.

Thus the complete lookup is:

```text
proven Sandbox
  -> current EgressBinding
  -> matching EgressPolicy rule
  -> presented placeholder / credential definition
  -> exact destination + presentation + audience + source
```

Unknown placeholders, multiple credential selections, unavailable sources, target mismatches, or
missing authoritative workload context fail closed. The agent-facing rules projection may disclose
the inert placeholder, destination, presentation shape, and non-secret description; it never
discloses a token or permits the agent to choose an audience/source.

### Do per-audience tokens help?

Not for the existing sidecar-to-central hop. A single short-lived `agentplane-egress` audience token
is sufficient because only the central proxy accepts it and the proxy never forwards it. Giving the
sidecar separate Action Service and LLM Proxy hop tokens would duplicate TokenReview and routing
without improving the destination's proof; it would also create more token-selection machinery.

Per-destination audiences do help when the credential is itself a KSA token presented to a service:
the Action Service token should not be accepted by LLM Proxy or the Kubernetes API, and vice versa.
Audience is defense in depth, not authorization by itself: the destination must still validate the
ServiceAccount subject and apply service policy/RoleBindings. A broadly authorized ServiceAccount
with many audiences remains broadly authorized.

## LLM Proxy and Action Service

For P0, both services are allowlisted internal destinations reached through the current sidecar and
central proxy. Each gets:

- the same proxy-derived workload-context envelope;
- a service-specific destination credential or authenticated proxy-to-service channel;
- its own exact EgressPolicy rule and credential target; and
- its own authorization over operations, independent of the egress allow decision.

The Action Service API must not accept `agent_id`, `sandbox_id`, `thread_id`, owner, or caller role as
proof. It records the proxy-derived origin and may accept caller-supplied correlation references only
as untrusted data checked against that origin. The LLM Proxy follows the same rule for model requests
and provider-native thread metadata.

The central proxy may substitute a configured Action Service or LLM Proxy credential exactly as it
does provider credentials. That is credential propagation, not impersonation: the credential has a
fixed source and target in configuration. If a service needs end-user/operator authentication, that
is a separate BFF/operator path and is not synthesized from a Sandbox hop token.

## Future native Kubernetes-service access

Some in-cluster services should eventually authenticate the actual Sandbox Pod ServiceAccount and
use ordinary Kubernetes RoleBindings. Preserve that identity without mounting a usable token in the
runner:

1. At Sandbox Pod creation, the trusted template/controller declares a finite set of projected
   ServiceAccount token volumes. Each projection fixes one destination audience, expiry, path, and
   the Pod's own ServiceAccount. Only the sidecar container mounts those paths.
2. Sidecar configuration binds each token path to one credential placeholder and one exact
   destination service/host/port/protocol. The agent can select only among already projected and
   Egress-bound credentials; it cannot supply an audience, ServiceAccount, token path, or arbitrary
   host.
3. The sidecar reads the selected projected token just in time and sends it to the central proxy in
   a dedicated destination-credential field, separate from `Proxy-Authorization`. This hop must be
   encrypted and authenticated; a plaintext header on the current sidecar-to-proxy connection is
   not an acceptable bearer-token transport.
4. The central proxy re-evaluates the live Sandbox binding and verifies that the received credential
   kind, placeholder, target, audience, and source slot match configuration. It then injects the
   token only into the exact intercepted request/metadata target and never exposes or logs it.
5. The destination validates the token for its own audience and authorizes the ServiceAccount via
   its normal RoleBindings or service policy.

This does not make the proxy an audience oracle: all audiences and projections are fixed before the
Pod starts; the proxy has no TokenRequest or ServiceAccount impersonation permission; the sidecar
cannot read an undeclared path; and the caller cannot widen destination or presentation. Adding an
audience requires an operator/controller Pod-template change and a new binding, not a runtime API
call.

### Required HTTPS CONNECT seam

The current sidecar sees only the CONNECT host and then pipes opaque TLS bytes; the central proxy
sees the intercepted inner request and placeholder. Therefore it cannot safely request an arbitrary
matching projected token from the sidecar after inspecting that inner request, and the sidecar cannot
select between multiple credentials for one CONNECT destination from inner headers or paths.

**Recommendation:** add a small authenticated sidecar-to-central relay protocol before native KSA
credential support. It must carry, independently, the hop token, an inert credential selector, and
at most one configured destination credential for one request or tunnel. Require a unique
placeholder/credential selection for each CONNECT tunnel; when standard clients cannot put the
selector on CONNECT, use an operator-configured dedicated local proxy listener/route for that
credential. Bind the central decision to the CONNECT host and every intercepted inner request; do
not permit HTTP/2 connection coalescing to widen the target. Encrypt the relay because it now carries
a destination bearer.

Two narrower alternatives are acceptable only when their constraint is explicit:

- select one credential solely by unique CONNECT `host:port`; simple, but unusable when a service
  shares a host across audiences/identities; or
- bypass CONNECT with a service-specific local adapter that emits an explicit authenticated request
  envelope; precise, but no longer transparent to ordinary HTTP clients.

Do not send all projected tokens on CONNECT, mount one in the runner, trust a caller-supplied
audience, or give the central proxy TokenRequest/impersonation authority. Those approaches turn a
bounded relay into a token oracle or move the bearer into the agent trust domain.

## Scope and acceptance gate

**P0 behavior now**

- accept this topology before merging any Action Service implementation;
- keep the existing loopback sidecar, `agentplane-egress` hop token, TokenReview/live
  Pod-to-Sandbox lookup, EgressPolicy/EgressBinding enforcement, TLS interception, and substitution;
- define one proxy-derived workload-context contract used by LLM Proxy and Action Service;
- configure exact, service-specific destination credentials/targets and service-owned authorization;
- prove in an integration test that direct runner-to-service calls and forged context fail, the
  runner sees placeholders only, and the two services receive the same proven Sandbox context.

**Needed support before native KSA destination credentials**

- fixed-audience sidecar-only projected volumes in the Sandbox Pod template;
- the encrypted/authenticated sidecar-to-central credential relay and CONNECT selection rule;
- destination validation and RoleBinding acceptance covering wrong audience, wrong Pod/Sandbox,
  stale Pod token, unbound placeholder, and mismatched host.

**Deferred**

- native Kubernetes-service destination tokens and RoleBinding rollout;
- authoritative Agent/Thread attribution until the existing control plane owns a current binding;
- external-agent authentication, standing grants, cross-agent data policy, and a general capability
  framework;
- a credential broker, dynamic TokenRequest service, identity registry, or generic impersonation
  layer.

Acceptance of this doc unblocks a new, rebased Action Service vertical slice. It does not make any
existing implementation PR merge-ready by itself.
