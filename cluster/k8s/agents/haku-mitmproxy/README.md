# Haku Mitmproxy Trust Management

This mirrors `../mitmproxy`, but uses a separate CA and proxy namespace for
`haku-sandbox`. It is Haku's egress fence — enforcement inventory #5 in
<../../../../haku/docs/security.md>.

- cert-manager creates `Secret/haku-mitmproxy-ca` in `haku-mitmproxy`.
- haku-mitmproxy mounts that Secret and builds mitmproxy's CA file from
  `tls.key` and `tls.crt`.
- Reflector mirrors the Secret into `cert-manager`, which is trust-manager's
  source namespace in this cluster.
- trust-manager writes `ConfigMap/haku-mitmproxy-ca-cert` into `haku-sandbox`
  **and `haku-ci`** (the Bazel image-build runner egresses through this same
  proxy — see `../../haku-ci/`).
- Kyverno mounts that ConfigMap into haku sandbox pods and points common TLS
  clients at `/mitmproxy-ca/ca-certificates.crt`. haku-ci mounts it directly in
  its Deployment (runner + dind) and injects it into job containers via the
  act_runner `container.options` (`config.yaml`).

## Build-dependency cache addon

`cache_addon.py` (mounted via `configMapGenerator`, backed by the
`haku-mitmproxy-cache` PVC) caches GET responses **only** from a strict allowlist
of immutable build-dependency hosts (Bazel registry/releases, GitHub archives,
npm, PyPI, nodejs.org, cache.nixos.org). This makes the haku-ci runner's Bazel
cold-fetch (no RBE) hit a local cache. It is default-deny: Haku's
credential-injected / API hosts are never in the allowlist, so no credentialed
response can enter the cache. `api.anthropic.com` is `--ignore-hosts`
(passthrough), so it never reaches the addon at all.

Keep haku CA rotation separate from the main sandbox mitmproxy CA. For planned
root rollover, trust both old and new roots before restarting haku-mitmproxy with
the new signing key.
