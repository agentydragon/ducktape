# CiliumNetworkPolicy in front of an HTTPRoute backend: use `fromEntities: [ingress]`

When you put a default-deny `CiliumNetworkPolicy` on a pod that's the backend of
a Gateway API `HTTPRoute` (Cilium gateway implementation), the only label/entity
that matches the cluster-gateway traffic is `reserved:ingress`. **Not** the
gateway pod's namespace, **not** the host node, **not** remote-node.

## Symptom

`HTTPRoute` is `Accepted` and `ResolvedRefs=True`. Service has endpoints. Pod
is Ready. But external requests through `https://<host>.allegedly.works` return
`503` from the gateway. Hubble shows repeated SYNs `(ingress) ... to-overlay
FORWARDED` arriving at the pod, but no completed handshake — the SYN-ACK reply
gets dropped on the way back because the policy doesn't recognize the source.

## Root cause

`cilium-envoy` runs `hostNetwork: true` on each node. Its egress to backend
pods uses the node's `cilium_host` interface IP (`10.244.<node>.196` style),
and **Cilium assigns these interface IPs the `reserved:ingress` identity** —
not `reserved:host`, not `reserved:remote-node`, and not the
`kube-system/cilium-envoy` pod identity.

Verify with:

```bash
NODE=$(kubectl get pod -n <ns> <backend-pod> -o jsonpath='{.spec.nodeName}')
kubectl exec -n kube-system "$(kubectl get pod -n kube-system -l app.kubernetes.io/name=cilium-agent --field-selector spec.nodeName="$NODE" -o name | head -1)" \
  -- cilium ip get <source-ip-from-hubble>/32
# IP                  IDENTITY           SOURCE
# 10.244.1.196/32     reserved:ingress
```

## Fix

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

## What I tried first that didn't work

1. `fromEndpoints: [{matchLabels: {k8s:io.kubernetes.pod.namespace: gateway-system}}]`
   — there are no pods in `gateway-system` (the `Gateway` resource is there but
   serving happens via the cilium-envoy DaemonSet in `kube-system`).
2. `fromEndpoints: [{matchLabels: {k8s:io.kubernetes.pod.namespace: kube-system,
k8s-app: cilium-envoy}}]` — closer (cilium-envoy is in kube-system) but
   still wrong because the pod identity doesn't apply to host-network egress.
3. `fromEntities: [host, remote-node]` — also wrong; cilium classifies the
   `cilium_host` interface IP as `reserved:ingress` independently of host/node.

## How to debug similar cases next time

1. `kubectl exec -n kube-system ds/cilium -- hubble observe --to-namespace <ns>`
   to see SYN forwards. If you see SYNs reaching the pod with no return flow
   visible, that's the policy-on-reverse-path case.
2. `cilium ip get <src>/32` from the destination node's cilium-agent to learn
   the identity Cilium assigns to that source IP.
3. Match the policy `fromEntities` (or `fromEndpoints`) to the actual identity.

Origin: manifold-mcp deployment, 2026-04-30. Three rebuild/redeploy cycles
spent figuring this out before I checked `cilium ip get`. Doing that first
would have saved ~30 minutes.
