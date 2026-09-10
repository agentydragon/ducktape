# kube-api-proxy

`kubeapi.allegedly.works:443` exposes the Kubernetes API for callers behind a
TLS-inspecting proxy. The Gateway terminates publicly trusted TLS; the nginx
proxy re-encrypts to the API server and preserves bearer authentication.

Direct client-certificate access uses `api.allegedly.works:6443`. There is no
API TLS-passthrough listener on port 443: it overlapped the Gateway's wildcard
HTTPS listener.

Haku's separately authorized API boundary uses `haku-kubeapi.allegedly.works`;
see <../haku/console/kube-api-proxy.yaml>.

## Why terminate TLS?

Claude Code web's egress proxy requires a publicly trusted server certificate.
The API server presents a cluster-CA-signed certificate. The Gateway presents the
wildcard Let's Encrypt certificate instead, and nginx verifies the API server
using the cluster CA. Client certificates do not survive this TLS termination;
callers authenticate with bearer JWTs.

## Topology

```text
Client → kubeapi.allegedly.works:443 (Gateway, public TLS certificate)
       → kubeapi-proxy:8080 (nginx)
       → kubernetes.default.svc:443 (API server, verified cluster-CA TLS)
```

## Resources

| File                      | Purpose                                      |
| ------------------------- | -------------------------------------------- |
| `httproute.yaml`          | Routes the public hostname to nginx          |
| `service.yaml`            | nginx configuration, Deployment, and Service |
| `kustomization.yaml`      | Resource list                                |
| `flux-kustomization.yaml` | Reconciliation and health checks             |

## kubectl exec / WebSocket

`kubectl exec`/`attach`/`port-forward` open an HTTP `Upgrade` (WebSocket, or SPDY
on older clients). The `kubeapi-proxy` nginx must forward that upgrade
(`proxy_http_version 1.1` together with the `Upgrade`/`Connection` headers — see
`service.yaml`); otherwise the apiserver returns `400 "Upgrade request required"`.
Root cause and isolation method for when this broke:
<../../docs/lessons_learned/2026_06_18_kubectl_exec_websocket_kubeapi_proxy.md>.
