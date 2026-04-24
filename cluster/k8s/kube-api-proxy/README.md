# kube-api-proxy

Two Gateway API routes exposing the in-cluster Kubernetes API on `*.allegedly.works:443`:

- `api.allegedly.works` — TLS **passthrough** (legacy, preserves end-to-end client-cert auth)
- `kubeapi.allegedly.works` — TLS **terminate** (new, for callers behind a TLS-inspecting proxy; bearer-JWT auth only)

## Why two routes?

Claude Code web sandboxes reach the internet through Anthropic's egress proxy,
which is an L7 TLS-terminating MITM: it presents an Anthropic-signed cert to
the client for every destination, then opens a fresh upstream TLS connection
and validates that upstream cert against public CAs.

- `api.allegedly.works` passes through to the apiserver's **cluster-CA-signed**
  cert. The Anthropic proxy fails public-CA validation on that upstream cert
  and returns `503 CERTIFICATE_VERIFY_FAILED`. So this route is unreachable
  from CC web. It still works for hosts outside the proxy (laptops — where
  the admin kubeconfig may point here — and in-cluster workloads).
- `kubeapi.allegedly.works` terminates the wildcard Let's Encrypt cert at the
  Cilium Gateway, which the Anthropic proxy is happy to validate, and then
  re-encrypts to the apiserver via a `BackendTLSPolicy`. Client certs die at
  the proxy boundary either way, so this route only supports bearer tokens
  (see `devinfra/claude/scripts/write_kubeconfig.py` for the Authentik
  client_credentials JWT flow).

See `cluster/docs/lessons_learned/2026_04_24_k8s_auth_through_mitm_proxy.md`
for the full investigation.

## Topology

### `api.allegedly.works` (passthrough)

```text
Client (outside Anthropic proxy)
   │ raw TLS (SNI: api.allegedly.works, client cert in handshake)
   ▼
Cilium Gateway (:443, `kube-api-passthrough` listener, mode=Passthrough)
   │ raw TLS stream forwarded unchanged
   ▼
kubernetes.default.svc.cluster.local:443 (apiserver, cluster-CA cert)
   ▲
   └─ x509 client cert validated here; O= maps to K8s group
```

Clients must trust the cluster CA (`secrets/k8s-ca.crt`).

### `kubeapi.allegedly.works` (terminate + re-encrypt)

```text
Client (possibly behind Anthropic L7 MITM proxy)
   │ HTTPS (Authorization: Bearer <jwt>)
   ▼
Cilium Gateway (:443, `https-wildcard` listener, mode=Terminate, LE wildcard)
   │ HTTP (JWT preserved in Authorization header)
   ▼ (BackendTLSPolicy re-encrypts, validates via kube-root-ca.crt ConfigMap)
   ▼
kubernetes.default.svc.cluster.local:443 (apiserver)
   ▲
   └─ JWT validated via AuthenticationConfiguration (kubectl-sandbox-client-credentials issuer)
      groups claim → oidc-ksbx-groups:kubectl-sandbox-users → existing RBAC
```

## Resources

| File                      | Purpose                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------- |
| `tlsroute.yaml`           | TLSRoute on `api.allegedly.works` to `kubernetes:443` (default namespace, passthrough)       |
| `httproute.yaml`          | HTTPRoute on `kubeapi.allegedly.works` to `kubernetes:443` (terminate at gateway)            |
| `backendtlspolicy.yaml`   | BackendTLSPolicy re-encrypting to the apiserver, validating via `kube-root-ca.crt` ConfigMap |
| `kustomization.yaml`      | Flux kustomization root                                                                      |
| `flux-kustomization.yaml` | Flux Kustomization (no `dependsOn`)                                                          |
