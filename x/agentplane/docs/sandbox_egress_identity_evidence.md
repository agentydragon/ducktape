# Sandbox egress identity evidence

A disposable spike, since removed (proof code and manifests at commit `8e74a1184b`), established
the identity boundary the egress proxy builds on. It ran on 2026-09-01 against the cluster at
Kubernetes v1.35.1 with Agent Sandbox controller v0.5.5 (v1beta1 `Sandbox`, `SandboxClaim` and
`SandboxTemplate`), Cilium-enforced NetworkPolicies, only the `nvidia` RuntimeClass, and no
SPIFFE/SPIRE or service mesh; SVID identity and Kata/gVisor/Firecracker isolation were therefore
not testable.

## What was tested and observed

Two standalone `Sandbox` CRs each produced one Ready Pod with independent `runner` and `proxy`
containers, `automountServiceAccountToken: false`, and only the proxy mounting a synthetic Secret
and a projected audience-scoped token. From the runner, credential-bearing environment variables,
the Secret and token paths, the default ServiceAccount path, the proxy process and the proxy's root
were all absent; the runner ServiceAccount could not `get pods`, and Cilium denied its connection
to `kubernetes.default.svc:443`. The containers shared neither PID namespace nor writable volume;
the runner reached the proxy's loopback socket, the intended path. Deliberate crashes of either
container wrote no credential or identity material to logs.

A verifier authenticated each proxy's token by TokenReview for a custom audience, read the
token-bound Pod live, required the connection source to equal the Pod's current IP, followed its
controller owner reference and matched the live Sandbox UID; the two Sandboxes resolved to distinct
Pod and Sandbox identities. A valid token replayed from one Sandbox's proxy through the other's was
rejected as a source-Pod mismatch; direct and forged runner requests were rejected as invalid
credentials and a forged forwarding header was ignored; a caller-chosen target was rejected because
the proxy has no generic forwarding API; a repeated nonce and a request older than 30 seconds were
rejected. Cilium denied the runner a connection to `1.1.1.1:80` while DNS and the verifier stayed
reachable.

Restarting either container alone left the Pod UID unchanged and the next request succeeded. A
Secret update was seen by proxy and verifier within seconds without a restart (projected-volume
reload by the test processes, not production rotation automation). Suspending and resuming a
Sandbox kept its UID and replaced the Pod: the old Pod-bound token was rejected and the replacement
Pod's token accepted — a stronger stale-object test than waiting out token expiry.

## What it does and does not establish

Ordinary runc mount/PID separation on this cluster, and authentication of a current Pod and its
Kubernetes owner — not VM-strength isolation from a hostile workload or kernel exploit, and not an
Agentplane Agent or Thread. Container-specific Secret mounts keep the value out of the runner under
the ordinary container boundary; a container escape, node compromise, or a future shared PID or
volume change would invalidate that. The source-IP correlation depends on Cilium preserving the
originating Pod IP and on verifier ingress staying limited to sandbox Pods; it is useful only with
TokenReview and live Pod UID lookup, not as portable cryptographic identity.

Not established:

- **Direct runner route denial with a proxy sidecar.** The runner opened TCP directly to the
  verifier. NetworkPolicy and Cilium identity apply to the Pod network namespace; they cannot allow
  the proxy container's egress while denying the runner's same-source traffic. Authentication
  stopped the direct operation; the route itself was not confined.
- **Native Agent Sandbox workload credentials.** v0.5.5 supplied Pod ownership, labels and status,
  not a verifier-facing Sandbox credential; Sandbox identity was derived from a Pod-bound token plus
  live owner resolution.
- **Durable anti-replay.** Nonces lived in verifier memory; a restart lost them. A production replay
  check needs durable state and a request-bound protocol, and mTLS alone would add neither Thread
  identity nor replay protection.
- **Independent per-container workload identity.** Both containers carry the Pod's ServiceAccount
  at the Kubernetes identity layer even though only the proxy receives a token.

## Adopted design

Agentplane now uses the trusted external gateway shape this experiment recommended. The local
sidecar has only an audience-scoped Pod token—not the real upstream credential:

```text
Agent -> unauthenticated local operation -> token-authenticated external gateway -> upstream
```

The central proxy uses TokenReview plus live Pod UID/source and Sandbox-owner checks, authorizes the
requested host/method/path, rejects forbidden addresses, and adds a real upstream credential only
when the selected rule requires one. Cilium policy allows the Sandbox Pod to reach DNS and this
proxy; only the proxy may reach protected upstreams. The current contract is
[the egress specification](../egress/SPEC.md), and deployed behavior is exercised by the
[acceptance suite](../acceptance/README.md).

The exact remaining gap is that sidecar and runner share one Pod network/Cilium identity. The Agent
can therefore open TCP directly to the gateway, but it cannot read the sidecar token, so the gateway
rejects the direct request. This is an application-authentication boundary, not forced traffic through
the sidecar. It is sufficient for the accepted threat model as long as the local API and gateway are
narrow capabilities rather than an arbitrary credential-redemption oracle. Durable replay control
and request-bound Agent/Thread assertions remain intentionally absent; add either only when a product
path requires that identity or freshness guarantee.
