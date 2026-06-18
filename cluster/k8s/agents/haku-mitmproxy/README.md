# Haku Mitmproxy Trust Management

This mirrors `../mitmproxy`, but uses a separate CA and proxy namespace for
`haku-sandbox`.

- cert-manager creates `Secret/haku-mitmproxy-ca` in `haku-mitmproxy`.
- haku-mitmproxy mounts that Secret and builds mitmproxy's CA file from
  `tls.key` and `tls.crt`.
- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/haku-mitmproxy-ca-cert` into `haku-sandbox`.
- Kyverno mounts that ConfigMap into haku sandbox pods and points common TLS
  clients at `/mitmproxy-ca/ca-certificates.crt`.

Keep haku CA rotation separate from the main sandbox mitmproxy CA. For planned
root rollover, trust both old and new roots before restarting haku-mitmproxy with
the new signing key.
