# CLIProxyAPI with management OIDC

Builds the backend from our OIDC branch rebased on CLIProxyAPI v8.0.63 and the
management UI from our OIDC fork. The OIDC changes originated in the now-closed
[CLIProxyAPI #6761](https://github.com/router-for-me/CLIProxyAPI/pull/6761) and
[Management Center #505](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/pull/505).
Exact revisions and checksums are in
`MODULE.bazel`. The backend uses `rules_go`; the frontend uses Vite through `rules_js`;
`rules_oci` packages both into a Debian 13 distroless image with CA certificates and
CGO runtime libraries. Go and npm dependencies are isolated from Ducktape's application
graphs.

```bash
bbr test //third_party/cli_proxy_api_tests:tests
bbr build @ducktape_cli_proxy_api//:image
```

The image publication roster publishes `cli-proxy-api` to the Forgejo registry.
The deployment is described in <../../cluster/k8s/cli-proxy-api/README.md>.

## Patch ownership

- `patches/bundled-panel.patch`: `MANAGEMENT_STATIC_READONLY=true` prevents panel
  downloads and replacement, including when the file is missing.
- `patches/bazel.patch`: BUILD additions applied after Gazelle generates upstream
  package definitions. This build glue is separate from the upstream feature.
- `patches/zstd-fixture.patch`: make the oversized single-segment zstd test fixture
  explicit for the newer compression library.
- `patches/upstream-timing.patch`: the access log line ends with
  `| upstream attempts=N sent=… headers=… first_chunk=… first_byte=…`, offsets from the
  request's arrival to the upstream request going out, its response headers, its first body
  chunk, and the first byte back to the client. It works with `request-log` off, so the
  split reaches Loki from stdout without writing request bodies anywhere.
- `patches/oidc-cookie-path.patch`: scopes the OIDC login and session cookies to their
  management routes, so same-origin model requests do not carry their values into request
  logs. The scoped cookies use the `__Secure-` prefix because `__Host-` requires `Path=/`.
  Management OIDC endpoints also expire any legacy root-scoped `__Host-` cookies they
  receive, using the original root path so browsers remove the matching entries.

Both source pins track forks because upstream has not merged OIDC support. Move the
backend and UI pins to upstream once equivalent support is available. Remove `bundled-panel.patch` when the upstream panel has an equivalent
read-only/no-download contract or the deployment no longer requires it. Remove
`bazel.patch` when the pinned upstream sources provide compatible BUILD metadata. Remove
`upstream-timing.patch` when upstream logs per-request upstream timing to stdout.
Each removal requires the test suite and image build below to pass.

Source revisions and checksums are in `MODULE.bazel`. The binary is installed at
`/CLIProxyAPI/CLIProxyAPI`; the UI is `/CLIProxyAPI/static/management.html`. The image
sets `MANAGEMENT_STATIC_PATH` and `MANAGEMENT_STATIC_READONLY` to serve that bundle.

## OIDC configuration

Unset `MANAGEMENT_OIDC_ISSUER` preserves key authentication. To enable OIDC, set:

| Environment variable               | Value                                                   |
| ---------------------------------- | ------------------------------------------------------- |
| `MANAGEMENT_OIDC_ISSUER`           | HTTPS OIDC issuer URL                                   |
| `MANAGEMENT_OIDC_CLIENT_ID`        | Registered client ID                                    |
| `MANAGEMENT_OIDC_CLIENT_SECRET`    | Confidential client secret, supplied from a Secret      |
| `MANAGEMENT_OIDC_REDIRECT_URL`     | `https://<admin-host>/v8/management/auth/oidc/callback` |
| `MANAGEMENT_OIDC_ALLOWED_SUBJECTS` | Comma-separated permitted `sub` values from that issuer |

The authorization-code flow uses PKCE, state bound to a browser cookie, nonce, and
ID-token signature/issuer/audience/expiry validation. Authorization requires an
allowed subject; display names and forwarded identity headers grant no access.
OIDC enables the management routes even without a management key. Existing remote
access restrictions still apply to management-key authentication.

Sessions are opaque, process-local, and bounded. They expire at the earlier of one
hour and ID-token expiry; restart revokes them. Configuration changes require a
restart. Use one replica or sticky routing. Provider access/refresh tokens are not
retained. Logout terminates the local session, not the identity-provider session.

Cookie-authenticated management requests require `X-Management-CSRF: 1`, reject
foreign origins, and reject cross-site fetches. Browser sessions authenticate only v8 management requests; legacy v0 requests
require a management key. Keep browser UI and management API on the same
origin. Explicit management keys continue to work without browser cookies. After
upgrading, a browser may retain an old root-scoped session cookie until its first
management session check/login/callback/logout or its prior expiry (at most one hour);
the server's process-local session store is revoked on restart.

## Updating upstream

Update the release or UI source pin and checksum, then rebase the remaining downstream
patches. Refresh
`go.mod`/`go.sum` from the patched backend, retaining the build module name, and
refresh the isolated pnpm lock with Bazel-managed pnpm when frontend dependencies
change. Frontend unit tests run the upstream Bun suite with a checksum-pinned runtime;
type checking and the Vite bundle still use Bazel-managed Node. **Gotcha:** `go.mod` omits modules that no package the Bazel targets reach
imports (gin's optional `sonic`, `jsoniter` and `go_json` JSON backends and their
transitive modules); do not restore them from upstream's `go.mod`. Recheck
`patches/bazel.patch` against Gazelle's generated BUILD files.
Run the test suite and image build above. When upstream releases contain the
features, update the source pins and remove the corresponding patches.
