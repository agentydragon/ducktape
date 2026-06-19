# 2026-06-18 — kubectl exec/attach/port-forward broken through kubeapi.allegedly.works

## Symptom

`kubectl exec` / `attach` / `port-forward` against `kubeapi.allegedly.works` (the
bearer-JWT TLS-terminate route Claude Code web sandboxes use) failed with an empty
`Error from server:`. Ordinary verbs (`get`/`logs`/`apply`/`delete`) worked.

With kubectl ≥1.31 (WebSocket-first exec) the verbose trace is `websocket: bad
handshake (400)` → SPDY fallback also fails → empty error.

## Root cause

The `kubeapi-proxy` nginx (`cluster/k8s/kube-api-proxy/service.yaml`), which bridges
the Cilium Gateway (TLS terminate) to the apiserver (HTTPS re-encrypt), was missing
WebSocket-upgrade config. Default nginx proxies as HTTP/1.0 and drops the hop-by-hop
`Upgrade`/`Connection` headers, so the apiserver received a plain `GET` to `/exec`
and returned `400 "Upgrade request required"`. `kubectl logs` survived because it's a
chunked HTTP response, not a connection upgrade.

## How it was isolated (cluster-side, not Anthropic's egress)

- The TLS cert for `kubeapi.allegedly.works` is issued by Anthropic's egress gateway
  (a TLS-terminating MITM) — but it forwards fine; `get`/`logs`/`apply` all work.
- A full-path exec probe returned `400` carrying an apiserver `Audit-Id` (only the
  apiserver stamps that), with auth accepted → the request reached the apiserver
  with the upgrade headers already stripped.
- An in-cluster probe straight to `kubeapi-proxy.default.svc:8080` — bypassing both
  the Anthropic egress and the Cilium Gateway — **still** returned `400 + Audit-Id`.
  With only nginx in the path, nginx is the stripper.

## Fix (PR #2376)

Add to the nginx config: `proxy_http_version 1.1`, `proxy_set_header Upgrade
$http_upgrade`, `proxy_set_header Connection $connection_upgrade`, and the
`map $http_upgrade $connection_upgrade { default upgrade; '' close; }` block. Plus
`reloader.stakater.com/auto: "true"` on the Deployment so the pods restart on the
ConfigMap change (the config is mounted via `subPath`, which doesn't hot-reload).
Verified: `kubectl exec` returns `101 Switching Protocols` and streams.

## Takeaway

Any nginx (or other L7 proxy) in front of the apiserver — or any WebSocket/SPDY
backend — needs the explicit `proxy_http_version 1.1` + `Upgrade`/`Connection`
directives. "Ordinary requests work but exec/attach/port-forward 400" — with an
apiserver `Audit-Id` on the 400 — is the signature of an upstream proxy silently
stripping the upgrade.
