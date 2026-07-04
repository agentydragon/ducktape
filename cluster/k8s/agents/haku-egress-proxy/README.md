# Haku Egress Proxy Trust Management

Haku's egress fence for `haku-sandbox` **and `haku-ci`** — enforcement inventory #5 in
<../../../../haku/docs/security.md>. The name is implementation-neutral on
purpose: it is **currently implemented with mitmproxy** (mirroring
`../mitmproxy`, but with a separate CA and proxy namespace), but the object
names deliberately avoid `mitmproxy` so the implementation can be swapped (e.g.
to Squid) without renaming the Namespace/Deployment/Service/CA wiring that other
manifests depend on.

- cert-manager creates `Secret/haku-egress-proxy-ca` in `haku-egress-proxy`.
- The proxy mounts that Secret and builds mitmproxy's CA file from
  `tls.key` and `tls.crt`.
- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/haku-egress-proxy-ca-cert` into `haku-sandbox`
  **and `haku-ci`** (the `Bundle` `namespaceSelector` in `trust-bundle.yaml`).
- Kyverno mounts that ConfigMap into haku sandbox pods and points common TLS
  clients at `/egress-proxy-ca/ca-certificates.crt`; `haku-ci` mounts it via its
  own Deployment/runner config (not Kyverno) — see <../../haku-ci/>.

Keep haku CA rotation separate from the main sandbox mitmproxy CA. For planned
root rollover, trust both old and new roots before restarting the egress proxy
with the new signing key.
