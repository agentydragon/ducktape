# Haku Squid Egress Fence

`haku-squid` is Haku's single egress fence — enforcement inventory #5 in
<../../../../haku/docs/security.md>. It replaces the former `haku-mitmproxy`.

It is a Squid `ssl-bump` caching forward proxy. It does everything mitmproxy did —
terminate + inspect client TLS, enforce a host allowlist, and splice
`api.anthropic.com` untouched — **plus** it caches immutable public build/package
artifacts. Both `haku-sandbox` (agent pods, via the Kyverno `inject-haku-squid`
policy) and `haku-ci` (the Bazelified image-build runner, hand-wired) egress
through it, so they share one HTTP cache. There is no credential injection.

## Trust flow

- cert-manager creates `Secret/haku-squid-ca` in `haku-squid`.
- The init container builds the ssl-bump signing CA (`squid-ca.pem` = `tls.key` +
  `tls.crt`), initializes the generated-leaf cert DB (`security_file_certgen`),
  and lays down the `cache_dir` swap structure on the PVC.
- Reflector mirrors the Secret into `cert-manager`, trust-manager's source
  namespace in this cluster.
- trust-manager writes `ConfigMap/haku-squid-ca-cert` into `haku-sandbox` **and**
  `haku-ci`. The bundle contains public roots, the cluster internal root, and the
  Squid root.
- Clients point their TLS trust at `/…/ca-certificates.crt`: Kyverno mounts it in
  haku-sandbox pods; haku-ci mounts it directly (runner + dind) and injects it
  into job containers via the act_runner `container.options`.

Keep haku CA rotation separate. For a planned root rollover, publish both old and
new roots in the trust bundle, wait for clients to see it, then restart haku-squid
with the new signing key.

## squid.conf design

- **ssl_bump**: `peek step1` → `splice anthropic` (raw passthrough for
  `api.anthropic.com`, never bumped, never cached) → `bump all` (decrypt, inspect,
  and cache everything else).
- **Host allowlist** (`allowed` dstdomain, default-deny): the union of the former
  mitmproxy allowlist and the Bazel/toolchain hosts. `http_access allow allowed` /
  `http_access deny all`.
- **Cache**: `cache_dir` on the 25Gi seaweedfs PVC. Credentialed / user-data hosts
  (Google APIs, Haku mailbox, the Docker token endpoint, Anthropic) are
  `cache deny` — no credentialed response can enter the cache. Immutable public
  hosts (registries, npm/pypi, nix, bazel/github/nodejs archives) are cached,
  respecting origin `Cache-Control` (no `override-expire` / `ignore-no-cache`, so
  `no-store`/`private` is always honored; non-200 and `Set-Cookie` responses are
  never cached).
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
confirm its AKI equals the CA SKI (`683d…`), and that `curl` gives `ssl_verify=0`.
