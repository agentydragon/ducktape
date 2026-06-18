# Mitmproxy Trust Management

This stack uses a dedicated mitmproxy root CA, separate from the cluster
internal CA:

- cert-manager creates `Secret/mitmproxy-ca` in `agents-mitmproxy`.
- mitmproxy mounts that Secret and builds `/mitmproxy-data/mitmproxy-ca.pem`
  from `tls.key` and `tls.crt`.
- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/mitmproxy-ca-cert` into the sandbox
  namespaces. The bundle contains public roots, the cluster internal root, and
  the mitmproxy root.
- Kyverno mounts that ConfigMap into sandbox pods and points common TLS clients
  at `/mitmproxy-ca/ca-certificates.crt`.

The CA is long-lived. Do not shorten the duration or force rotation without an
overlap plan: first publish both the old and new roots in the trust-manager
bundle, wait for sandbox pods to see the updated bundle, then restart mitmproxy
with the new signing key. After all clients trust the new root, remove the old
root from the bundle.
