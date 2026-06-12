# kubeapi.allegedly.works backend TLS: Cilium doesn't support it (2026-04-25)

**Date**: 2026-04-25
**Status**: Fixed — nginx reverse proxy replaces BackendTLSPolicy.

## Symptom

Claude Code web reported all kube-apiserver paths returning HTTP 404 with
empty body via `kubeapi.allegedly.works`. The JWT rotation pipeline (fixed
earlier this session) produced valid JWTs, but the apiserver never received
the requests.

## Root cause

The `kubeapi.allegedly.works` HTTPRoute (created 2026-04-24 in
<2026_04_24_k8s_auth_through_mitm_proxy.md>) relied on a `BackendTLSPolicy`
to re-encrypt traffic from the Cilium Gateway to the apiserver. **Cilium
doesn't implement `BackendTLSPolicy`** — the CRD exists because Gateway API
CRDs are installed as a set, but the Cilium gateway controller silently
ignores it.

Result: the gateway terminated TLS (wildcard LE cert), then sent **plain HTTP**
to `kubernetes:443`. The apiserver responded with
`Client sent an HTTP request to an HTTPS server.`, which Envoy couldn't parse
and returned 404 to the client.

## Approaches tried and failed

### 1. BackendTLSPolicy (original design)

Created `BackendTLSPolicy` resource referencing `kube-root-ca.crt` ConfigMap
for upstream CA validation. Resource was accepted (valid CRD) but had no
`.status` — Cilium never processed it. The apiserver received plain HTTP.

See <../../k8s/kube-api-proxy/> for the original design in the previous commit.

### 2. `appProtocol: https` via GEP-1911

Created a wrapper Service (`kubeapi-https`) with `appProtocol: https` on the
port, plus a static `EndpointSlice` targeting the CP node IPs. Enabled
`gatewayAPI.enableAppProtocol: true` in Cilium Helm values.

Result: Cilium accepted the config and generated an Envoy cluster for the
service, but **did not add a TLS transport socket** to the upstream cluster
definition. Envoy still connected as plain HTTP. The apiserver responded with
400 `Client sent an HTTP request to an HTTPS server.` (progress from 404,
but still broken).

Cilium's `enableAppProtocol` appears to only support `kubernetes.io/h2c`
(HTTP/2 cleartext) for protocol selection, not `https` for backend TLS.

### 3. CiliumEnvoyConfig cluster override

Created a `CiliumEnvoyConfig` in the `default` namespace to override the
auto-generated Envoy cluster with a `transportSocket` containing an
`UpstreamTlsContext`. The CEC was created successfully but **the gateway
controller's auto-generated CEC** (in `gateway-system`) takes precedence —
the cluster definition without TLS won out.

### 4. nginx reverse proxy (working solution)

Deployed a 2-replica nginx pod (`nginxinc/nginx-unprivileged:1.27-alpine`)
that listens on plain HTTP port 8080 and proxies to
`https://kubernetes.default.svc:443` using the auto-mounted ServiceAccount
CA cert for upstream validation. The HTTPRoute points to this Service instead
of directly to `kubernetes`.

```text
Client → Anthropic proxy → Cilium Gateway (LE cert, terminate)
  → kubeapi-proxy nginx (HTTP 8080) → kubernetes.default.svc (HTTPS 443)
```

This works because:

- Cilium can route plain HTTP to an HTTP backend (no TLS needed)
- nginx handles the HTTP→HTTPS bridge
- The Authorization header passes through unchanged
- The ServiceAccount CA cert (`/var/run/secrets/.../ca.crt`) is available
  in every pod for free

## Lessons

- **Check that your Gateway API implementation supports the features you use.**
  The CRD existing doesn't mean the controller implements it. Cilium's Gateway
  API support covers GatewayClass, Gateway, HTTPRoute, GRPCRoute, TLSRoute,
  and ReferenceGrant — but NOT BackendTLSPolicy. Always verify with the
  controller's docs, not the CRD presence.

- **`appProtocol` in Cilium is for protocol selection, not backend TLS.**
  `enableAppProtocol` enables choosing between HTTP/1.1 and HTTP/2 cleartext
  (`kubernetes.io/h2c`), not HTTPS re-encryption.

- **Auto-generated CiliumEnvoyConfig wins over manual overrides.** The gateway
  controller continuously reconciles its CEC. Manual CEC resources in other
  namespaces can't override cluster definitions owned by the gateway.

- **nginx reverse proxy is the simplest HTTP→HTTPS bridge.** When the gateway
  can't re-encrypt, a lightweight proxy pod is more reliable than trying to
  patch Envoy config. Two replicas, ~16Mi RAM, negligible CPU.

## Files

| File                                      | Purpose                                         |
| ----------------------------------------- | ----------------------------------------------- |
| <../../k8s/kube-api-proxy/service.yaml>   | nginx proxy Deployment + Service + ConfigMap    |
| <../../k8s/kube-api-proxy/httproute.yaml> | HTTPRoute → kubeapi-proxy:8080                  |
| <../../k8s/kube-api-proxy/README.md>      | Topology and design rationale                   |
| <../../terraform/main/cilium-values.yaml> | `enableAppProtocol: true` (kept for future use) |
