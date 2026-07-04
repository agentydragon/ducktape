# CiliumNetworkPolicy gotchas

## Gateway API backends must admit `reserved:ingress`

A default-deny `CiliumNetworkPolicy` in front of a pod that is the backend of a
Gateway API `HTTPRoute` (Cilium gateway implementation) must admit
`reserved:ingress` — **not** the gateway pod's namespace, **not** `reserved:host`,
**not** `reserved:remote-node`.

### Why

`cilium-envoy` runs `hostNetwork: true` on each node. Its egress to backend pods
uses the node's `cilium_host` interface IP, and Cilium assigns those interface
IPs the `reserved:ingress` identity — not `reserved:host`, not `reserved:remote-node`,
and not the `kube-system/cilium-envoy` pod identity. Any other selector drops the
SYN-ACK on the return path, so external requests through
`https://<host>.allegedly.works` return `503` even though the `HTTPRoute` is
`Accepted`, `ResolvedRefs=True`, the Service has endpoints, and the pod is
`Ready`.

### Pattern

```yaml
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/name: <backend>
  ingress:
    - fromEntities:
        - ingress
      toPorts:
        - ports:
            - port: "<facade-port>"
              protocol: TCP
```

Live example: <../k8s/agents/manifold-mcp/app/networkpolicy.yaml>.

### Debugging a mis-classified source

1. `kubectl exec -n kube-system ds/cilium -- hubble observe --to-namespace <ns>` —
   SYN forwards reaching the pod with no return flow indicate a reverse-path
   policy drop.
2. `cilium ip get <source-ip>/32` from the destination node's cilium-agent
   reveals the identity Cilium assigned to that source.
3. Match the policy `fromEntities`/`fromEndpoints` to the actual identity.

Origin: manifold-mcp deployment (2026-04-30). Full incident write-up:
<../../debug/manifold_mcp_cnp_cilium_envoy_identity.md>.

## Egress to a ClusterIP: allow the backend targetPort, not the Service port

A port-restricted egress rule to an in-cluster Service must list the backend
**`targetPort`**, not the Service `port`. With kube-proxy replacement, Cilium's
socket-LB rewrites `ClusterIP:port → podIP:targetPort` in the `connect()` hook —
**before** L4 egress policy is enforced — so the policy only ever sees the
translated backend port.

### Symptom

The client's TCP connection to the Service times out ("connection refused"/dial
timeout / `Client.Timeout ... while awaiting connection`), even though the egress
rule allows the Service's advertised port and DNS resolves fine. A pod with
unrestricted egress (all ports, or `toEntities: cluster` with no `toPorts`) reaches
the same Service without trouble — which is the tell that it's a port, not a
routing/DNS, problem.

### Example

`oci-cache`'s Service is `:80 → targetPort 5000`. Exposing it on `:80` did **not**
let `haku-ci`'s `toEntities: cluster` rule (ports `80/443/3000`) reach it — the
policy had to allow **5000**, the pod's container port. See
<../k8s/haku-ci/ccnp-force-proxy-egress.yaml>.

### Debugging

`kubectl exec -n kube-system ds/cilium -- hubble observe --from-namespace <ns>
--type drop` shows the dropped egress flow with the actual (translated) destination
port — compare that to the ports your `toPorts` allows.

Origin: oci-cache pull-through mirror wiring (2026-07-04).
