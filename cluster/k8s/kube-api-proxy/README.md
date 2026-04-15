# kube-api-proxy

nginx reverse proxy that exposes the in-cluster Kubernetes API on
`https://api.allegedly.works` (port 443) via the Cilium Gateway API.

## Why

Claude Code web sandboxes (and any other CCR v2 environment) reach the
internet through Anthropic's TLS-inspecting egress proxy, which only
permits port 443 outbound — non-standard ports like the apiserver's
`:6443` (or the previous `:16443` hostPort listener) are silently
filtered. Hook daemons running inside web sessions need the k8s API to
write per-session kubeconfigs and (optionally) fetch the OTEL bearer
token, so the apiserver must be reachable on `:443`.

This proxy fronts the in-cluster `kubernetes.default.svc.cluster.local`
Service, listens plaintext on `:8080`, and re-encrypts upstream over
TLS. The Cilium Gateway terminates TLS for the wildcard cert
`*.allegedly.works` on `:443` and forwards plain HTTP to the Service.

## Topology

```text
Internet
   │ TLS, *.allegedly.works wildcard cert
   ▼
Cilium Gateway (:443, hostNetwork on hil VPS)
   │ HTTP (plain)
   ▼
Service kube-api-proxy/kube-api-proxy (ClusterIP :80 → :8080)
   │
   ▼
Deployment kube-api-proxy/kube-api-proxy (nginx, 2 replicas, hil-pinned)
   │ HTTPS (proxy_ssl on, SNI = kubernetes.default.svc.cluster.local)
   ▼
kubernetes.default.svc.cluster.local:443 (apiserver)
```

## Resources

| File                      | Purpose                                                     |
| ------------------------- | ----------------------------------------------------------- |
| `namespace.yaml`          | `kube-api-proxy` namespace                                  |
| `deployment.yaml`         | nginx Deployment (2 replicas, hil-pinned, no hostNetwork)   |
| `service.yaml`            | ClusterIP `:80` → pod `:8080`                               |
| `httproute.yaml`          | Gateway API HTTPRoute on `cluster-gateway/https-wildcard`   |
| `config/nginx.conf`       | nginx config (plain http reverse proxy with WS upgrade map) |
| `kustomization.yaml`      | Flux kustomization root + ConfigMap generator               |
| `flux-kustomization.yaml` | Flux Kustomization (no `dependsOn`, health: Deployment)     |

## Future direction

Once the cluster is on Cilium ≥ 1.20, this Deployment can be replaced
by a direct HTTPRoute → `kubernetes.default` backend with a
`BackendTLSPolicy` to handle the upstream TLS handoff. Cilium 1.19.2
(currently deployed) does not support `BackendTLSPolicy`, which is why
the nginx hop is still required.

The full rationale and lifecycle notes live in the `description`
annotation on `deployment.yaml` — keep them in sync.
