# ADR: credentialless Sandbox egress through a Pod-bound sidecar token and external gateway

- **Status:** Accepted and implemented by Agentplane's secure egress integration
- **Date:** 2026-09-01
- **Scope:** Agentplane's Kubernetes/Sandbox egress composition
- **Scope boundary:** This design does not make the current staging path ready to carry production
  credentials; freshness, replay resistance, rotation, and escape tests remain separate gates.

## Decision

When Agentplane lets an Agent use an upstream service without giving the Agent a real credential, use
this composition:

```text
Agent / runner
    |
    | ordinary HTTP(S) proxy request on loopback
    v
per-Sandbox proxy sidecar
    |
    | audience-scoped, Pod-bound Kubernetes ServiceAccount token
    v
trusted external gateway
    |
    | TokenReview + live Pod/Sandbox correlation
    | host/method/path/address and credential policy
    | real upstream credential substituted here only
    v
allowlisted upstream service
```

The sidecar receives no real upstream credential. It receives only the short-lived Kubernetes token
needed to assert its Pod workload to the gateway. The gateway is the credential-bearing and final
authorization boundary. It validates the token through Kubernetes TokenReview, checks the live Pod UID
and (where the deployment path preserves it) source Pod IP, follows the controller owner to the live
Sandbox, and then authorizes the request against explicit rules before making the upstream request.

Once Agentplane exists, its authoritative mapping can extend the gateway decision from:

```text
Pod -> Sandbox
```

to:

```text
Pod -> Sandbox -> Agentplane Thread/Agent
```

That Thread/Agent binding is a later Agentplane integration concern. It must not be implied merely by
Kubernetes workload authentication.

## Problem and constraints

We wanted all of the following:

- keep real upstream credentials outside the Agent's trust domain;
- let a downstream service identify which live Sandbox workload is calling;
- support per-Sandbox/per-Thread provisioning without warm-pool reassignment ambiguity;
- use native Kubernetes and Agent Sandbox authorities where possible;
- keep the local relay credentialless and make the central capability narrow enough that an Agent
  cannot turn it into a credential-redemption oracle; and
- preserve a path to request freshness, replay control, and eventual Thread binding.

The relevant network constraint is structural: a Kubernetes Pod is the usual network-policy identity.
Cilium and NetworkPolicy selectors generally authorize the Pod's traffic, not individual containers in
the Pod. A runner and a proxy sidecar share the Pod network namespace, Pod IP, and network identity.
Therefore a policy cannot generally allow the sidecar to reach a gateway while denying the runner's TCP
route to that same gateway.

The spike tested this directly. The runner could open TCP to the verifier/gateway route, but the direct
application request was rejected because the runner did not possess the sidecar's hidden Pod token and
upstream credential. The result is application-authenticated capability use, not literal forced routing
through the sidecar.

## Evidence behind the decision

The retired disposable proof, preserved as
[sandbox egress identity evidence](sandbox_egress_identity_evidence.md), established on the
pinned cluster:

- only the proxy mounted the synthetic upstream Secret and projected audience-scoped Pod token;
- the runner had no Secret/token mount, Kubernetes API authority, proxy process visibility, or proxy-root
  access under the tested runc boundary;
- TokenReview plus live Pod UID/IP and Sandbox owner lookup distinguished two Sandboxes;
- a copied token from Sandbox A to Sandbox B was rejected;
- replay, forged headers, invalid credentials, proxy/runner restart, Secret reload, and Pod replacement
  were exercised; and
- Sandbox suspension/resume retained the Sandbox UID but replaced the Pod UID, causing the old
  Pod-bound token to fail.

The proof also recorded what it did **not** establish: same-Pod route confinement, Thread identity,
durable replay protection, SPIFFE/SPIRE availability, or VM-strength isolation.

## Why this shape, and not the tempting alternatives

### Not a real upstream token in the Agent or runner

This directly violates the desired confidentiality invariant. Environment variables, workspace files,
native harness frames, logs, crash output, and process memory are all poor locations for a credential
when the Agent can inspect the runner's security domain.

### Not a real upstream credential in the sidecar

A sidecar is better isolated from the runner than a shared process, but it remains inside the Sandbox
Pod's broader trust and operational boundary. Keeping the valuable credential at the external gateway
reduces the blast radius of a compromised or misconfigured Sandbox and leaves the sidecar with only a
short-lived workload assertion.

### Not NetworkPolicy/Cilium alone

NetworkPolicy is still required for route reduction and defense in depth, but it cannot provide
container-level route separation in this Pod composition and does not put authenticated workload
identity into an application request. It is not enough as the sole control.

### Not an unrestricted external forward proxy

The local sidecar is an ordinary HTTP(S) relay, but it holds no upstream credential and forwards only
to the central proxy. The central proxy is not unrestricted: an Agent can select only hosts, methods,
paths, addresses, and credential presentations admitted by its bindings. This keeps the sidecar
general enough for unmodified tools without turning the credential-bearing boundary into an
exfiltration, signing, or credential-redemption oracle.

### Not forwarded headers or source IP as the sole identity

Headers supplied by the Agent are forgeable. Source IP can be useful as a correlation check in a known
Cilium path, but it is not portable cryptographic identity and may represent a gateway/NAT path. Use
it only alongside TokenReview and live Pod/Sandbox lookup.

### Not SPIFFE/SPIRE as the complete answer

SPIFFE/SPIRE is a candidate workload-authentication layer, not a complete Agentplane identity design.
It does not automatically establish the Agentplane Thread, request freshness, replay resistance, or
real-secret exclusion. It was not installed in the spike cluster, so adopting it remains unnecessary
for this decision.

### Not mTLS alone

mTLS can authenticate a workload or gateway peer, but it does not by itself say which Thread is active,
prevent an authorized workload from repeating a request, or keep a credential out of the Agent's trust
domain if key material is readable there.

### Not runner-mediated credential handoff as the default

A trusted runner could receive a short-lived credential and pass it to the proxy over a private
one-shot channel, but this is safe only if the runner is genuinely isolated from Agent-controlled code.
Otherwise it merely moves the secret into another inspectable process. Treat it as a fallback when
proxy-only Secret mounting is impossible, not as the default architecture.

### Not warm-pool adoption or a shared template credential

Warm pools are not part of the near-term design. A template-level Secret reference is shared by every
Pod using that template and cannot express unique per-Thread credentials. If unique credentials are
ever needed, the provisioning path must bind a per-Sandbox/per-Thread Secret or equivalent gateway
capability before startup.

### Not Firecracker/Kata/gVisor immediately

A stronger runtime may eventually be justified by a threat model involving container escape or node
compromise, but the driver protocol should not depend on it. The current evidence demonstrates useful
credential exclusion under ordinary container boundaries; it does not justify adding a stronger runtime
before that threat is concrete.

## Consequences

### Benefits

- The Agent remains credentialless with respect to real upstream secrets.
- The gateway receives a native, short-lived Kubernetes workload assertion rather than trusting Agent-
  supplied identity fields.
- Pod replacement and Sandbox owner checks provide useful stale-object behavior.
- Agentplane can later add authoritative Pod/Sandbox/Thread binding without changing native harness
  semantics.
- The proxy and gateway can be tested as narrow capabilities before integrating real upstream services.

### Costs and known limitations

- The runner can still reach the gateway at the network layer because of shared Pod identity.
- The gateway must be Kubernetes-aware or call an authority that is; this is not a portable standalone
  HTTP authentication scheme.
- TokenReview and live object lookup add latency and availability dependencies.
- Source-IP correlation is deployment-specific.
- Durable replay control and request-bound Thread assertions still need implementation if required.
- Ordinary runc isolation is not a VM-strength boundary.

## Remaining follow-up

1. Add durable replay/request freshness only when the product path requires it; do not mistake the
   spike's in-memory nonce set for production protection.
2. Add request-body policy only when a granted operation needs a narrower request shape than
   host/method/path and credential-presentation rules provide.
3. Bind a gateway request to the authoritative Pod/Sandbox/Thread mapping
   only if the downstream actually needs Thread identity.
4. Revisit gVisor/Kata/Firecracker only if measured threat or escape evidence exceeds ordinary
   container/sidecar isolation.
