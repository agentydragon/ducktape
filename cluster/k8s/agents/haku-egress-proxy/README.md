# Haku Egress Proxy Trust Management

Haku's egress fence for `haku-sandbox` **and `haku-ci`** — an enforcement inventory
entry in <../../../../haku/docs/security.md>. The name is implementation-neutral on
purpose: it is **currently implemented with mitmproxy** (mirroring
`../mitmproxy`, but with a separate CA and proxy namespace), but the object
names deliberately avoid `mitmproxy` so the implementation can be swapped (e.g.
to Squid) without renaming the Namespace/Deployment/Service/CA wiring that other
manifests depend on.

- cert-manager creates `Secret/haku-egress-proxy-ca` in `haku-egress-proxy`.
- The shared proxy mounts that Secret and builds mitmproxy's CA file from
  `tls.key` and `tls.crt`.
- The retired Haku OpenClaw spike's credential-substituting Iron listener and its
  Claude OAuth token are removed. This shared CA and proxy remain for Haku sandbox
  and Haku CI.
- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/haku-egress-proxy-ca-cert` into `haku-sandbox`
  **and `haku-ci`** (the `Bundle`
  `namespaceSelector` in `cluster/cdk8s/haku_egress_proxy.py`).
- `haku-ci` mounts that ConfigMap at `/egress-proxy-ca/ca-certificates.crt` via its runner
  config — see <../../../cdk8s/haku_ci/runner.py>. Nothing in `haku-sandbox` uses this proxy:
  its workloads egress through Agentplane egress sidecars.

Keep haku CA rotation separate from the main sandbox mitmproxy CA. For planned
root rollover, trust both old and new roots before restarting the egress proxy
with the new signing key.
