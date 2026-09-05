# Sandbox egress identity evidence

Observed on 2026-09-01 against `admin@talos-cluster`: Kubernetes v1.35.1, Agent Sandbox controller
v0.5.5, and the v1beta1 `Sandbox`, `SandboxClaim`, and `SandboxTemplate` CRDs. Cilium enforced the
NetworkPolicies. Only the `nvidia` RuntimeClass was installed; no SPIFFE/SPIRE or service-mesh CRDs
were present.

The disposable proof that produced this evidence was removed after Agentplane's production-shaped
egress proxy, sidecar, Kubernetes resources, and deployed acceptance suite adopted its conclusions.
Git commit `8e74a1184b` preserves the exact proof code and manifests. Its final run started from an
absent namespace, printed `all_proofs=passed`, exited 0, and removed the namespace plus its
TokenReview ClusterRole and binding. The identifiers below are observations, not stable
configuration.

## Proven

### Sandbox composition and credential exclusion

- Two standalone `Sandbox` CRs each produced one Ready Pod with independent `runner` and `proxy`
  containers. No warm pool, `SandboxClaim`, or production template was involved.
- The Pod set `automountServiceAccountToken: false`. Only the proxy mounted the synthetic Secret and
  a projected token volume. The runner mounted only its code and private scratch volume.
- From the runner, credential-bearing environment variables, the Secret path, projected-token path,
  default ServiceAccount path, proxy process, and proxy-root Secret path were all absent. Kubernetes
  still injected the non-secret `KUBERNETES_SERVICE_HOST` address.
- The runner ServiceAccount had no `get pods` authorization, and Cilium denied the runner's TCP
  connection to `kubernetes.default.svc:443`.
- The containers did not share a PID namespace or writable volume. The runner could reach the proxy's
  loopback socket, which is the intended operation path.
- The proxy and runner wrote zero bytes to their previous-container logs across deliberate crashes;
  no synthetic credential or identity proof appeared in their output.

These observations establish ordinary runc container mount/PID separation on this cluster. They do
not establish VM-strength isolation from a hostile workload or kernel exploit.

### Fixed operation, workload identity, and attacks

- The fixed proxy operation returned 200 for both Sandboxes. The verifier reported distinct Pod and
  Sandbox identities:
  - A: Pod `0ccb476e-7bd6-4fd8-850d-d09fc7e0f0a4`, Sandbox
    `db0dfc41-7d50-487a-b626-1eab7c3d4ece`.
  - B: Pod `65c6fda2-b9ba-449b-8944-2d1e01b8c43d`, Sandbox
    `9ae09edc-fce7-4304-9392-140cdb8cb930`.
- TokenReview authenticated the namespace/ServiceAccount and the token-bound Pod name/UID for the
  custom `agentplane-sandbox-proxy-spike` audience. The verifier then read that live Pod, required the
  connection source to equal its current Pod IP, followed its controller owner reference, and matched
  the live Sandbox UID.
- A valid token copied directly from A's proxy to B's proxy without printing or storing it was
  rejected 403 as `source_pod_mismatch`.
- Direct and forged runner requests were rejected 401 as `invalid_upstream_credential`. A forged
  forwarding header was ignored.
- Adding an arbitrary target to the proxy operation was rejected 400 as `invalid_fields`; the proxy
  has no generic forwarding or caller-selected destination API.
- Repeating one accepted request nonce was rejected 409 as `replayed_request`. A timestamp older than
  30 seconds is also rejected by the committed verifier.
- Cilium denied a runner TCP connection to `1.1.1.1:80`, while DNS and the verifier's port remained
  reachable.

This authenticates a current Pod and its Kubernetes owner mapping. It does not authenticate an
Agentplane Agent or Thread.

### Lifecycle

- A deliberate proxy exit incremented only the proxy restart count to 1. A subsequent operation
  succeeded with the same Pod UID.
- A deliberate runner exit incremented only the runner restart count to 1. A subsequent operation
  succeeded with the same Pod UID.
- Updating the synthetic Secret from version `v1` to `v2` was observed by both proxy and verifier
  after seven three-second polling attempts. The next accepted result reported `v2`; neither process
  needed a restart. This proves projected-volume reload by the test processes, not production rotation
  automation.
- Suspending and resuming Sandbox A retained Sandbox UID
  `db0dfc41-7d50-487a-b626-1eab7c3d4ece` while replacing Pod UID
  `0ccb476e-7bd6-4fd8-850d-d09fc7e0f0a4` with
  `f1c46739-cfc7-4a4c-b407-c1e33a416d11`. The old Pod-bound token was rejected 401 as
  `workload_token_rejected`; the replacement Pod's token and rotated Secret then succeeded.

## Unsupported

- **Direct runner route denial with a proxy sidecar.** The runner successfully opened TCP directly to
  the protected verifier. NetworkPolicy and Cilium identity apply to the Pod network namespace; they
  cannot allow the proxy container's egress while denying the runner container's same-source traffic.
  Authentication stopped the direct operation, but the route itself was not confined.
- **Native Agent Sandbox workload credentials.** v0.5.5 supplied Pod ownership, labels, and status,
  not a verifier-facing Sandbox credential. The experiment derived Sandbox identity by combining a
  Kubernetes Pod-bound token with live Pod owner resolution.
- **Agent or Thread binding.** No Agentplane Agent/Thread exists in the spike, and neither Kubernetes
  nor Agent Sandbox supplied one.
- **Durable anti-replay.** Nonces are held in verifier memory. A verifier restart loses them; the
  application check is not a durable request ledger.
- **Independent per-container workload identity.** Both containers have the Pod's ServiceAccount at
  the Kubernetes identity layer even though only the proxy receives a token.

## Blocked by environment

- SPIFFE/SPIRE X.509-SVID was not testable because SPIRE was not installed. Installing a cluster-wide
  identity system was outside the authorized spike scope.
- Kata, gVisor, and Firecracker isolation were not testable because no such RuntimeClass was installed.
- Token expiry was not delayed for a separate ten-minute observation. Pod replacement provided the
  stronger relevant stale-object test: deletion invalidated the old bound token immediately.
- Thread archive behavior was not testable because Thread persistence is an explicit non-goal.

## Inferred

- The source-IP correlation depends on current in-cluster Cilium routing preserving the originating
  Pod IP and on verifier ingress remaining limited to selected sandbox Pods. It is useful only in
  combination with TokenReview and live Pod UID lookup; it is not portable cryptographic identity.
- Container-specific Secret mounts keep the value out of the runner under the ordinary Kubernetes
  container boundary. A container escape, node compromise, or unsafe future shared PID/volume change
  would invalidate that conclusion.
- A production replay check needs durable state and a request-bound protocol. mTLS alone would not add
  Thread identity or application replay protection.

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
