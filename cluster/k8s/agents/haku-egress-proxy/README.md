# Haku Egress Proxy Trust Management

Haku's egress fence for `haku-sandbox` **and `haku-ci`** — enforcement inventory #5 in
<../../../../haku/docs/security.md>. The name is implementation-neutral on
purpose: the shared listener is **currently implemented with mitmproxy**
(mirroring `../mitmproxy`, but with a separate CA and proxy namespace), but the
object names deliberately avoid `mitmproxy` so the implementation can be swapped
without renaming the Namespace/Deployment/Service/CA wiring that other manifests
depend on. Three further listeners in this namespace already run iron-proxy,
which is where the shared one is headed too — what blocks each remaining
consumer is listed in `//cluster/validation:test_egress_allowlists`.

- cert-manager creates `Secret/haku-egress-proxy-ca` in `haku-egress-proxy`.
- The shared proxy mounts that Secret and builds mitmproxy's CA file from
  `tls.key` and `tls.crt`.
- `haku-claude-oauth-proxy` is a separate iron-proxy deployment using the same CA.
  It alone receives the real Claude subscription OAuth token and replaces the
  non-secret sandbox placeholder only for `api.anthropic.com`'s `Authorization`
  header. Only `haku` access-profile runner pods in `haku-runtime-sandbox` may connect to its
  listener; other profile runners and Haku's general `haku-sandbox` compute authority cannot use
  the subscription proxy. The shared mitmproxy remains unchanged.
- `haku-openclaw-spike-proxy` is a second isolated iron-proxy listener for the
  OpenClaw compatibility spike. It substitutes separate placeholders for Claude
  OAuth, Haku's Forgejo password, and the Haku Console bearer, each scoped to its
  exact destination host. Only `haku-openclaw-spike` may connect to it.
- `haku-sandbox-iron-proxy` is a third iron-proxy listener, for the exec-target
  sandboxes Haku reaches through `exec_sandbox`. It splits `haku-sandbox` in two:
  pods carrying `app.kubernetes.io/name: haku-sandbox` (the `haku`
  SandboxTemplate stamps it) reach the internet through this listener, everything
  else in the namespace through the shared mitmproxy. Cilium enforces the split
  (`ccnp-haku-{proxy,sandbox-box}-egress.yaml`) and a matching pair of Kyverno
  policies sets each half's `HTTP_PROXY`.

  It substitutes nothing yet, which is the point of the next step rather than an
  oversight. Both credentials the box holds — the Forgejo password and the Haku
  Console bearer — are spent on `forgejo-http.forgejo` and on
  `haku-kube-api-proxy…svc.cluster.local`, and `NO_PROXY` sends both straight
  past the proxy, so there is no request for a `secrets` transform to fire on.
  Taking `*.forgejo` out of `NO_PROXY` is what unblocks the first of them, and
  the injector's own comment records that the entry exists only because
  mitmproxy buffers chunked git packs — iron does not. The account's other name,
  `git.allegedly.works`, is bypassed for an unrelated reason (the public-Gateway
  hairpin), so the box keeps the real password until both names route through a
  proxy or collapse into one (`haku/TODO.md`).

- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/haku-egress-proxy-ca-cert` into `haku-sandbox`,
  `haku-runtime-sandbox`, `haku-openclaw-spike`, **and `haku-ci`** (the `Bundle`
  `namespaceSelector` in `trust-bundle.yaml`).
- Kyverno mounts that ConfigMap into haku sandbox pods and points common TLS
  clients at `/egress-proxy-ca/ca-certificates.crt`; `haku-ci` mounts it via its
  own Deployment/runner config (not Kyverno) — see <../../haku-ci/>.

Keep haku CA rotation separate from the main sandbox mitmproxy CA. For planned
root rollover, trust both old and new roots before restarting the egress proxy
with the new signing key.
