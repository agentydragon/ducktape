# Sandbox egress identity: option survey

This is an unscheduled research note for a possible future Agentplane capability. It does not add a
requirement to the first credentialless Agentplane service.

Confidence labels:

- **Documented/current:** public documentation or current released behavior.
- **Documented/proposed:** published roadmap or design intent, not a current guarantee.
- **Inference:** a plausible composition, not a documented product contract.

## Decision update

The practical prototype was run against the pinned cluster and its result underlies the implemented
credentialed networking setup. A per-Sandbox local HTTP(S) relay holds only an audience-scoped,
Pod-bound Kubernetes token; the trusted central proxy validates that token through TokenReview plus
live Pod/Sandbox correlation and keeps real upstream credentials outside the Sandbox. The central
proxy, not same-Pod NetworkPolicy, is the final application-authentication boundary.

The prototype also confirmed the important limitation: runner and proxy share the Pod network identity,
so the runner can reach the gateway at TCP level. It cannot make an accepted protected request without
the sidecar-held token. The local relay is generic but credentialless; the central proxy admits only
explicitly bound hosts, methods, paths, addresses, and credential presentations. This is recorded in
[ADR: credentialless Sandbox egress](adr_sandbox_proxy_gateway.md)
and the [observed identity evidence](../docs/sandbox_egress_identity_evidence.md).

The decision does not claim Thread identity, durable replay protection, SPIFFE/SPIRE availability, or
VM-strength isolation. Agentplane can later add Pod -> Sandbox -> Thread/Agent binding once it owns that
mapping.

## Finding

No single Kubernetes or network mechanism proves all of:

- traffic originated in a specific Sandbox;
- that Sandbox currently hosts a specific Agentplane Thread;
- the request is fresh and not replayed; and
- the real upstream credential never entered the Agent's trust domain.

A credible later design will probably compose four controls:

1. NetworkPolicy/CNI policy forces sandbox egress through a trusted path.
2. Workload identity authenticates a Pod- or workload-level principal.
3. A trusted proxy, sidecar, or gateway holds and substitutes the real upstream credential.
4. If Thread identity and replay resistance are required, a trusted control plane issues a
   short-lived proof bound to its authoritative Agent/Thread/Sandbox mapping and, where needed, the
   request.

The first credentialless Agentplane service should not implement this in anticipation of later needs.

## Claude Code Web pattern

**Documented/current:** Anthropic describes Claude Code Web as running in an isolated managed
sandbox/VM with outbound traffic mediated by security proxies. For Git operations, the real GitHub
token stays outside the sandbox. Git inside uses a custom scoped credential; the proxy validates the
credential and requested repository/branch/destination before attaching the real GitHub token.

This is strong evidence for the credentialless-Agent proxy pattern: the untrusted environment gets a
narrow capability, while a trusted external component holds and redeems the valuable credential.

Public sources:

- <https://www.anthropic.com/engineering/claude-code-sandboxing>
- <https://code.claude.com/docs/en/sandbox-environments>
- <https://docs.anthropic.com/en/docs/claude-code/security>
- <https://platform.claude.com/docs/en/agent-sdk/secure-deployment>

**Not established publicly:** that Claude Code Web specifically uses Firecracker. Anthropic mentions
Firecracker as a deployment option, not as disclosure of Claude Code Web internals. Firecracker can
strengthen VM isolation, but it does not itself provide Thread identity, workload credentials, or a
credential broker.

This pattern also does not prove per-request anti-replay or that every capability is absent from the
VM: a scoped credential still exists inside.

## Kubernetes and workload-identity options

### NetworkPolicy

**Documented/current.** Restricts L3/L4 reachability using Pod/namespace selectors and IP blocks. It
can make a trusted proxy the only reachable tool/egress path.

It does not put cryptographic Sandbox or Thread identity into the application request and does not
keep credentials out of the sandbox by itself.

Source: <https://kubernetes.io/docs/concepts/services-networking/network-policies/>

### CNI label identity and egress gateways

**Documented/current.** Systems such as Cilium derive policy identities from Kubernetes labels and
can route selected workloads through an egress gateway or stable source IP.

This is useful enforcement and observability, but a downstream MCP service does not automatically
receive end-to-end proof of the original Pod identity. Source IP usually proves the gateway or NAT
pool, not a Thread. Label identities may also be shared by multiple Pods.

Sources:

- <https://docs.cilium.io/en/stable/security/policy/intro/>
- <https://docs.cilium.io/en/stable/network/egress-gateway/>

### Bound Kubernetes ServiceAccount tokens

**Documented/current.** A projected, audience-scoped ServiceAccount token can be short lived and bound
to a Pod/object. A downstream service can validate it through TokenReview and inspect Pod metadata
claims.

This is comparatively native, but it places a usable bearer inside the sandbox. Possession does not
prove the request is currently originating there, and a copied token can be replayed until expiry.
The ordinary authorization principal remains the ServiceAccount, not an Agentplane Thread.

Sources:

- <https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/>
- <https://kubernetes.io/docs/reference/kubernetes-api/authentication-resources/token-review-v1/>

### SPIFFE/SPIRE mTLS

**Documented/current.** SPIRE can attest workloads and issue short-lived X.509-SVIDs. A downstream
service can authenticate a SPIFFE ID and key holder through mTLS.

This is the strongest general-purpose workload-identity candidate surveyed, but it proves a Sandbox
only if registration and attestation assign a unique Sandbox identity. Common selectors describe a
namespace, ServiceAccount, or label set—not a Thread. Application requests may still be repeated by
an authorized workload. If Agent-controlled code can read the Workload API or key material, the key
is not excluded from the Agent trust domain.

Sources:

- <https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/>
- <https://spiffe.io/docs/latest/deploying/spire_about/>

### Service mesh mTLS

**Documented/current.** Meshes such as Istio can authenticate workload principals between proxies,
commonly using SPIFFE-style namespace/ServiceAccount identities.

This may be attractive if a mesh already exists. It is not automatically a unique Pod/Sandbox or
Thread identity, and an external destination often sees the egress gateway's identity unless source
identity is separately conveyed and protected.

Source: <https://istio.io/latest/docs/concepts/security/>

### Trusted egress or service gateway

**Inference.** A gateway could authenticate the source workload internally, enforce destination/tool
policy, hold real credentials, and authenticate upstream. This matches the Claude Code Web pattern.

The downstream normally sees the gateway's identity, so the gateway must derive, validate, and audit
the original Sandbox/Thread context itself. Forwarded identity headers are trustworthy only when
untrusted callers cannot reach the destination directly or forge the headers.

## Agent Sandbox roadmap

**Documented/proposed:** the Agent Sandbox project describes “rich identity and connectivity” work,
including dual user/sandbox identities, routing without a Service per Sandbox, and dynamic
Sandbox/Pod identity association at claim time—particularly for pre-warmed pools.

These are directly relevant, but they are roadmap statements rather than current guarantees. We
should inspect the implementation and roadmap again when this capability is scheduled instead of
building a competing identity system now.

Sources:

- <https://github.com/kubernetes-sigs/agent-sandbox>
- <https://github.com/kubernetes-sigs/agent-sandbox/blob/main/roadmap.md>
- <https://agent-sandbox.sigs.k8s.io/docs/getting_started/overview/>

“Agent Gateway” should also be disambiguated at that time: similarly named gateway projects can
provide policy and credential mediation, but are not necessarily part of Agent Sandbox or an
authoritative Sandbox/Thread identity service.

## Trusted-runner boundary

A token in the harness-runner process is viable only when that runner is a genuinely trusted process
isolated from the Agent. Process memory is not a security boundary if Agent-controlled code shares
its user, PID/IPC namespace, inspectable `/proc`, writable filesystem, inherited file descriptors,
local sockets, debugging capabilities, or core/crash outputs.

A future trusted runner, sidecar, or gateway must therefore answer both:

- what prevents the Agent from extracting its credential or proof; and
- what prevents the Agent from using its narrow local protocol as an unrestricted signing or
  credential-redemption oracle?

## Candidate later prototype sequence

When this becomes important:

1. **Roadmap/current-state spike:** inspect the pinned Agent Sandbox version, roadmap issues, identity
   association implementation, router/gateway work, and cluster CNI capabilities.
2. **State the exact principal:** decide whether the verifier needs Agent, Thread, Sandbox Claim, Pod,
   runner instance, or per-request capability identity.
3. **Confinement proof:** in a disposable namespace, prove NetworkPolicy allows the sandbox to reach
   only one trusted proxy and blocks direct access to the protected service.
4. **Workload-proof comparison:** test a Pod-bound Kubernetes token and SPIFFE mTLS; document exactly
   which Pod/Sandbox metadata the verifier can authenticate and what is replayable.
5. **Credential-exclusion proof:** keep a synthetic real credential only in the proxy, verify the
   sandbox can perform one allowed operation, and scan environment, filesystem, `/proc`, native
   frames, logs, and crash paths for leakage.
6. **Thread binding only if required:** have Agentplane mint a very short-lived, audience-scoped
   assertion from its authoritative Thread/Sandbox mapping, preferably proof-of-possession or
   request-bound rather than an ordinary reusable bearer.
7. **Escape tests:** attempt direct egress, forged forwarded identity, copied proof from another
   sandbox, replay, stale Thread use, Pod replacement, and proxy-oracle abuse.

A successful prototype should report separately whether it proves workload identity, Thread
identity, freshness, route confinement, and real-secret exclusion. “Authenticated” is too vague to
serve as the acceptance result.
