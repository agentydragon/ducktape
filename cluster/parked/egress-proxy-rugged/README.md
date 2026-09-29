# Egress Proxy: rugged

Internal HTTP CONNECT proxy pinned to the `rugged` Kubernetes node.

Clients opt in explicitly:

```bash
HTTPS_PROXY=http://egress-rugged.egress-proxy.svc.cluster.local:8080
HTTP_PROXY=http://egress-rugged.egress-proxy.svc.cluster.local:8080
NO_PROXY=.svc,.cluster.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

For Playwright, configure the browser proxy directly instead of relying only on
environment variables:

```js
proxy: {
  server: 'http://egress-rugged.egress-proxy.svc.cluster.local:8080',
}
```

This is intentionally one `Deployment` pinned to one node for deterministic
experiments. A later pooled service can be built from a DaemonSet once multiple
egress nodes are ready.
