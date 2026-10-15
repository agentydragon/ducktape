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
    | TokenReview + Pod-bound ServiceAccount identity
    | host/method/path/address and credential policy
    | real upstream credential substituted here only
    v
allowlisted upstream service
```

The sidecar receives no real upstream credential. It holds short-lived, audience-scoped Kubernetes
tokens for the gateway and configured destinations. The gateway is the credential-bearing and final
authorization boundary. It validates the token through Kubernetes TokenReview and authorizes the
ServiceAccount identity the API server bound to the Pod. TokenReview returns the Pod name and UID;
the API server rejects a token bound to a deleted or replaced Pod. The current egress gateway does
not check the connection source address or resolve the Pod's Sandbox owner.

The gateway's principal is a Pod-bound ServiceAccount, not a Sandbox or Thread. A destination that
needs Sandbox or Thread identity must resolve it through that identity's owning authority; it cannot
infer it from workload authentication.

## Problem and constraints

We wanted all of the following:

- keep real upstream credentials outside the Agent's trust domain;
- let a downstream service authenticate the Pod-bound ServiceAccount making the call;
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

## Evidence behind the decision

### Identity boundary and historical evidence

A retired disposable spike (proof code and manifests at commit `8e74a1184b`) ran on 2026-09-01 with
Kubernetes v1.35.1, Agent Sandbox controller v0.5.5 and Cilium-enforced NetworkPolicies. It tested an
ordinary runc Pod with a runner and proxy sidecar; this lab evidence is not a claim about a current
cluster rollout or VM isolation.

In that setup, only the proxy mounted the synthetic Secret and audience-scoped Pod token; the runner
could not inspect the proxy's process or root under the tested runc boundary. The runner could open
TCP to the verifier from the same Pod, but a direct request without the proxy token was refused. This
confirms an application-authentication boundary, not container-level route confinement: both
containers shared a Pod network namespace and Cilium identity.

The spike's custom verifier used TokenReview claims, source-Pod-IP comparison and Sandbox-owner UID
lookup; it rejected a copied token presented for the other Sandbox. Those source and owner checks are
historical test behavior, not the current egress gateway contract. The production proxy relies on
TokenReview's Pod-bound identity and authorizes the ServiceAccount subject; it does not inspect source
IP or follow a Sandbox owner. Current behavior is specified in the [egress contract](../egress/SPEC.md#identity).

The spike also exercised restarts and Pod replacement. It did not establish durable anti-replay: its
nonce set was in verifier memory and was lost on restart. The current gateway has no request-bound
nonce or freshness check, and does not reject duplicate client submissions. A copied Pod-bound token
can be presented from elsewhere in the cluster while it remains valid, subject to that ServiceAccount's
egress rules. The containers also share Kubernetes Pod and ServiceAccount identity; the spike did not
establish SPIFFE/SPIRE availability or VM-strength isolation.

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

Headers supplied by the Agent are forgeable. Source IP can be a deployment-specific correlation check,
but it is not portable cryptographic identity and may represent a gateway/NAT path. The current egress
gateway does not use it; TokenReview supplies the Pod-bound identity that this contract authorizes.

### Not SPIFFE/SPIRE as the complete answer

SPIFFE/SPIRE is a candidate workload-authentication layer, not a complete Agentplane identity design.
It does not automatically establish the Agentplane Thread, request freshness, replay resistance, or
real-secret exclusion, so it would not replace this gateway design.

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
compromise, but the driver protocol should not depend on it. The retired spike showed credential
exclusion under its ordinary container boundary; it did not establish VM isolation or a reason to add
a stronger runtime before that threat is concrete.

## Consequences

### Benefits

- The Agent remains credentialless with respect to real upstream secrets.
- The gateway receives a native, short-lived Kubernetes workload assertion rather than trusting Agent-
  supplied identity fields.
- TokenReview rejects a Pod-bound token after its Pod is deleted or replaced.
- Agentplane can later add authoritative Pod/Sandbox/Thread binding without changing native harness
  semantics.
- The proxy and gateway can be tested as narrow capabilities before integrating real upstream services.

### Costs and known limitations

- The runner can still reach the gateway at the network layer because of shared Pod identity.
- The gateway must be Kubernetes-aware or call an authority that is; this is not a portable standalone
  HTTP authentication scheme.
- TokenReview adds a Kubernetes API availability dependency. The current egress gateway does not
  correlate the caller's source address or resolve its Sandbox owner.
- Durable request replay control and request-bound Thread assertions are absent; implement them only
  when a product path requires those guarantees.
- Ordinary runc isolation is not a VM-strength boundary.

## Remaining follow-up

1. Add request-body policy only when a granted operation needs a narrower request shape than
   host/method/path and credential-presentation rules provide.
2. Add durable replay protection or bind requests to an authoritative Sandbox/Thread mapping only if
   a product path requires those guarantees.
3. Revisit gVisor/Kata/Firecracker only if measured threat or escape evidence exceeds ordinary
   container/sidecar isolation.
