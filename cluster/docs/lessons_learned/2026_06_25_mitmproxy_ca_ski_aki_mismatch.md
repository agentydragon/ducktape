# mitmproxy <12.2.3 + cert-manager CA: SKI/AKI algorithm mismatch breaks all intercepted TLS

**Symptom.** Every pod in a mitmproxy-intercepted sandbox (`haku-sandbox`, and the
claude-sandbox equivalent) fails TLS verification to **all** intercepted hosts:

```text
curl: (60) ... unable to get local issuer certificate
ssl_verify=20
```

even though the mitmproxy CA is present in the pod's trust bundle and
`CURL_CA_BUNDLE`/`SSL_CERT_FILE` point at it. mitmproxy's own log shows
`Client TLS handshake failed ... the client does not trust the proxy's certificate`.

**Root cause — two different Subject Key Identifier algorithms.** The CA key,
cert, and secret are all valid and matching. The break is purely in key-identifier
computation:

- **cert-manager** mints the CA cert's `SubjectKeyIdentifier` per **RFC 7093** —
  the leftmost 160 bits of **SHA-256** of the public key (FIPS-friendly; Go
  ≥1.25 and cert-manager ≥1.18 default to this). For our EC CA that is
  `68:3d:3e…`.
- **mitmproxy <12.2.3** stamps each generated leaf's `AuthorityKeyIdentifier`
  with `x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key())`,
  which **recomputes** the keyid as **SHA-1** (RFC 5280 method 1) = `20:10:e9…`
  — it does **not** copy the CA cert's actual SKI.

So every leaf says "issued by a CA with keyid `20:10`", but the CA cert's SKI is
`68:3d`. openssl/curl link a leaf to its issuer by matching `AKI.keyid → issuer
SKI`; `20:10 ≠ 68:3d` ⇒ issuer not found ⇒ error 20, for every connection. It
never worked; it did not "drift".

**The tell.** Loading the confdir CA showed `key_SKI=20:10` and `cert_SKI=68:3d`
but `key_matches_cert=True`. Same key — two SKI **algorithms**:
`SubjectKeyIdentifier.from_public_key` (SHA-1) = `20:10`; the cert's embedded SKI
(RFC 7093 SHA-256-trunc) = `68:3d`. Confirmed: `sha256(EC point)[:20]` == the
embedded `68:3d`.

**Fix.** Bump the proxy to **mitmproxy ≥12.2.3** ([mitmproxy#8214](https://github.com/mitmproxy/mitmproxy/pull/8214)),
which copies the CA's SKI into the leaf AKI (falling back to SHA-1 only when the
CA has no SKI). Verified live: on 12.2.3 the leaf AKID becomes `68:3d…`,
`openssl verify` against the bundle returns `OK`, and `curl` gives
`ssl_verify=0`. No CA-side changes; cert-manager keeps managing the CA.
Both `cluster/k8s/agents/{haku-mitmproxy,mitmproxy}` pinned the floating `:11`
tag (→ 11.1.3) and were affected.

**Non-fixes ruled out (so we don't re-chase them):**

- **Reloader / CA rotation** — the secret hadn't rotated; nothing to reload.
  (`reloader.stakater.com/auto` also can't see this secret: it's mounted only in
  the init container, an auto-mode blind spot — true but irrelevant here.)
- **EC vs RSA** — mitmproxy loads and signs with the EC CA fine; the SKI method
  is independent of key type, so RSA wouldn't have helped.
- **Transient key/cert mismatch / stale process** — restarting/re-seeding the
  confdir changed nothing; the signer was never the problem.
- **cert-manager knob** — no feature gate controls SKI computation in v1.19.4 or
  master/v1.20+; the industry is moving _toward_ RFC 7093 (Let's Encrypt did).
- **Static SHA-1-SKI CA in the secret** — would work (matches mitmproxy's
  recompute) but is unnecessary once mitmproxy honors the CA's SKI.

**Generalizes to:** any cert-manager-issued CA fed to a tool that derives leaf
AKIs by recomputing SHA-1 rather than copying the issuer SKI.
