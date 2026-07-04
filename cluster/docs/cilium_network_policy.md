# CiliumNetworkPolicy patterns for Gateway API backends

A default-deny `CiliumNetworkPolicy` in front of a pod that is the backend of a
Gateway API `HTTPRoute` (Cilium gateway implementation) must admit
`reserved:ingress` — **not** the gateway pod's namespace, **not** `reserved:host`,
**not** `reserved:remote-node`.

## Why

`cilium-envoy` runs `hostNetwork: true` on each node. Its egress to backend pods
uses the node's `cilium_host` interface IP, and Cilium assigns those interface
IPs the `reserved:ingress` identity — not `reserved:host`, not `reserved:remote-node`,
and not the `kube-system/cilium-envoy` pod identity. Any other selector drops the
SYN-ACK on the return path, so external requests through
`https://<host>.allegedly.works` return `503` even though the `HTTPRoute` is
`Accepted`, `ResolvedRefs=True`, the Service has endpoints, and the pod is
`Ready`.

## Pattern

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

## Debugging a mis-classified source

1. `kubectl exec -n kube-system ds/cilium -- hubble observe --to-namespace <ns>` —
   SYN forwards reaching the pod with no return flow indicate a reverse-path
   policy drop.
2. `cilium ip get <source-ip>/32` from the destination node's cilium-agent
   reveals the identity Cilium assigned to that source.
3. Match the policy `fromEntities`/`fromEndpoints` to the actual identity.

Origin: manifold-mcp deployment (2026-04-30). Full incident write-up:
<../../debug/manifold_mcp_cnp_cilium_envoy_identity.md>.
