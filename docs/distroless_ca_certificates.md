# System CA certificates in distroless images

`rules_distroless` unpacks the `ca-certificates` deb but never runs
`update-ca-certificates`, so its apt `:flat` layer ships only loose certs under
`/usr/share/ca-certificates` and **no** `/etc/ssl/certs/ca-certificates.crt` bundle. A
system-trust TLS client then fails to verify. The single repo-level fix is the shared
`cacerts` layer — never re-assemble a bundle at runtime, and never rely on the apt layer
alone.

Add the shared layer to the `oci_image` `tars` (one target for both bookworm- and
trixie-based images — the bundle is release-independent PEM):

```python
tars = [
    "@my_apt//:flat",
    "//third_party/debian_slim:cacerts",
    ":layers",
],
```

Then, by TLS client:

- **C `git` / `curl` / OpenSSL CLI**: the bundle lands at the default path, so they find
  it automatically. Also set `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` (via
  `py_image_env(extra_env=...)`) for any OpenSSL consumer.
- **`pygit2` / `libgit2`**: it ignores `SSL_CERT_FILE`/`GIT_SSL_CAINFO`; call
  `pygit2.settings.set_ssl_cert_locations("/etc/ssl/certs/ca-certificates.crt", None)`.
- **Python `requests`/`httpx`, Rust `reqwest` (rustls)**: these bundle their own roots
  (`certifi` / webpki), so they need **no** CA layer at all.
- **Python `httpx2`** (FastMCP 4 / mcp-sdk v2's unconditional HTTP client, see
  `nix/packages/httpx2.nix`) **depends on `truststore`, not `certifi`.** On Linux
  `truststore` has no native trust-store API to call, so it falls back to OpenSSL's own
  default-verify-paths lookup — a system-trust client in disguise. It needs the `cacerts`
  layer and `SSL_CERT_FILE` exactly like the CLI case above; do not extend the
  `requests`/`httpx` exemption to it.
