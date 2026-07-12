# Haku Egress Proxy

`haku-egress-proxy` is Haku's single egress fence for `haku-sandbox` **and `haku-ci`** —
enforcement inventory #5 in <../../../../haku/docs/security.md>.

The k8s object names are implementation-neutral (`haku-egress-proxy`, not
`haku-squid`) so a future proxy swap doesn't cascade renames. **It is currently
implemented with Squid** — the `squid.conf`, the `ubuntu/squid` image, and
squid-specific paths (`/etc/squid`, `squid-ca.pem`) are the only Squid-named
artifacts; everything else is generic.

It is a Squid `ssl-bump` caching forward proxy. It does everything mitmproxy did —
terminate + inspect client TLS, enforce a host allowlist, and splice
`api.anthropic.com` untouched — **plus** it caches immutable public build/package
artifacts that haku-sandbox's ad-hoc `pip`/`npx`/nix installs and the Bazelified
`haku-ci` build pull. `haku-sandbox` pods reach it via the Kyverno
`inject-haku-egress-proxy` policy; `haku-ci` reaches it via a force-proxy CCNP plus
its own runner config (see <../../haku-ci/>). Both set `HTTP_PROXY`/`HTTPS_PROXY`
to `haku-egress-proxy.haku-egress-proxy.svc:8080` and mount the CA bundle. There is
no credential injection.

## Trust flow

- cert-manager creates `Secret/haku-egress-proxy-ca` in `haku-egress-proxy`.
- The init container builds the ssl-bump signing CA (`squid-ca.pem` = `tls.key` +
  `tls.crt`), initializes the generated-leaf cert DB (`security_file_certgen`),
  and lays down the `cache_dir` swap structure on the PVC.
- Reflector mirrors the Secret into `cert-manager`, trust-manager's source
  namespace in this cluster.
- trust-manager writes `ConfigMap/haku-egress-proxy-ca-cert` into `haku-sandbox`
  **and `haku-ci`** (the `Bundle` `namespaceSelector` in `trust-bundle.yaml`). The
  bundle contains public roots, the cluster internal root, and the Squid root.
- Kyverno mounts that ConfigMap into haku-sandbox pods and points common TLS
  clients at `/egress-proxy-ca/ca-certificates.crt`; `haku-ci` mounts it via its
  own Deployment/runner config (not Kyverno) — see <../../haku-ci/>.

The CA is the same cert-manager `Certificate`/`Secret` the mitmproxy
implementation used; the swap only changes which process consumes it (Squid's
`security_file_certgen` instead of mitmproxy) and how leaves are minted. Keep haku
CA rotation separate. For a planned root rollover, publish both old and new roots
in the trust bundle, wait for clients to see it, then restart haku-egress-proxy
with the new signing key.

## squid.conf design

- **ssl_bump**: `peek step1` → `splice anthropic` (raw passthrough for
  `api.anthropic.com`, never bumped, never cached) → `bump all` (decrypt, inspect,
  and cache everything else).
- **Host allowlist** (`allowed` dstdomain, default-deny): the union of the
  haku-sandbox set (image registries, pypi/npm, nix, Google APIs, the Haku mailbox,
  Anthropic) and the `haku-ci` Bazel/toolchain + Forgejo hosts. `http_access allow
allowed` / `http_access deny all`. This set MUST equal the pod-egress CNP's
  `toFQDNs` (cnp-haku-cloud-api-egress.yaml) — enforced by
  <../../../validation/test_haku_egress_proxy_consistency.py>.
- **Cache**: `cache_dir` on the 25Gi seaweedfs PVC. Credentialed / user-data hosts
  (Google APIs, Haku mailbox, the Docker token endpoint, Anthropic) are
  `cache deny` — no credentialed response can enter the cache. Immutable public
  hosts (registries, npm/pypi, nix, Bazel/toolchain) are cached, respecting origin
  `Cache-Control` (no `override-expire` / `ignore-no-cache`, so `no-store`/`private`
  is always honored; non-200 and `Set-Cookie` responses are never cached).
- **Audit**: `access_log` to stdout — the L7 audit trail that replaces the mitmweb
  UI (`kubectl logs` / Loki).

## Deviation: leaf-cert AKI/SKI

Standard cert-manager-issued CA fed to a bumping proxy. **Gotcha carried from the
mitmproxy incident** (<../../../docs/lessons_learned/2026_06_25_mitmproxy_ca_ski_aki_mismatch.md>):
cert-manager mints the CA's `SubjectKeyIdentifier` per RFC 7093 (truncated
SHA-256), and a strict client links a leaf to its issuer by
`leaf.AKI.keyid == issuer.SKI`. Squid's `security_file_certgen` must therefore
stamp each generated leaf's `AuthorityKeyIdentifier` with the CA cert's **actual**
SKI (OpenSSL's `keyid` method copies the issuer's SKI extension when present),
NOT a recomputed SHA-1. **Verify** after first deploy: extract a bumped leaf and
confirm its AKI equals the CA SKI, and that `curl` (via the injected trust bundle)
validates the chain without `-k`.
