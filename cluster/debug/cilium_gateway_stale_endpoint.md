# Cilium Gateway served a recycled Pod IP for agentplane-staging

Observed 2026-09-11 while `https://agentplane-staging.allegedly.works/` answered
`{"detail":"Not Found"}` and its `/healthz` body and response headers matched the Haku
console.

## Observed

- `HTTPRoute agentplane-staging/agentplane-staging` → `Service agentplane-app:8080`
  (ClusterIP `10.99.12.140`), Accepted and ResolvedRefs.
- The only `agentplane-app` Pod (`10.244.2.18`) was `0/1` Ready for two hours: its image
  predates the `/readyz` route its readiness probe names (the probe change landed in
  #6183 ahead of the image), and the app Kustomization that would roll the image was
  blocked behind `agentplane-staging-actions`.
- Cilium's BPF service map listed the backend as `10.244.2.18:8080 (maintenance)`.
- The Gateway Envoy cluster `gateway-system/cilium-gateway-cluster-gateway/agentplane-staging:agentplane-app:8080`
  listed endpoint `10.244.2.97:8080` (`cx_connect_fail` 18, `rq_error` 19), the IP of the
  previous `agentplane-app` Pod, and by then of `haku-console/haku-console-789f9c5b7f-djb8p`
  on the same node.
- Requests for the staging hostname, cookies included, were therefore proxied to the
  Haku console.

## Inference

With no Ready endpoint, the Gateway's endpoint set for the backend was not emptied but
kept its last Ready member, and the Pod IP allocator reused that address for a Pod of
another namespace. The Gateway thus crosses a namespace boundary on IP reuse whenever a
backend has zero Ready endpoints. The CiliumNetworkPolicy on the receiving Pod did not
stop it: Gateway traffic arrives from the `ingress` identity, which that policy admits.

## Confirmed after recovery

With two Ready `agentplane-app` Pods (10:24 UTC), the same Envoy cluster listed exactly
their IPs, both `healthy`, and a deleted replica's IP left the list within seconds of its
replacement becoming Ready. The retention only occurs while the backend has zero Ready
endpoints, and no Envoy restart was needed to clear it.

Open: whether a clean scale-to-zero reproduces the retention, which separates a Cilium
bug (stale EDS on the transition to zero Ready endpoints) from an artifact of the earlier
crash-loop. Check `cilium/cilium` for an open issue on Gateway API EDS retaining endpoints
after the last Ready endpoint leaves before filing.
