# CLIProxyAPI with management OIDC

Builds pinned upstream backend and management UI sources with local patches. The
backend uses `rules_go`; the frontend uses Vite through `rules_js`; `rules_oci`
packages both into a Debian 13 distroless image with CA certificates and CGO runtime
libraries. Go and npm dependencies are isolated from Ducktape's application graphs.

```bash
bbr test //third_party/cli_proxy_api_tests:tests
bbr build @ducktape_cli_proxy_api//:image
```

The image publication roster publishes `cli-proxy-api` to the Forgejo registry.
The existing deployment is not switched by this package; the migration is described
in <../../cluster/k8s/cli-proxy-api/README.md>.

## Patch ownership

- `patches/management-oidc.patch`: optional native OIDC management login and its
  security tests. Model API keys and explicit management keys remain supported.
- `patches/frontend.patch`: browser-session discovery, SSO login, local logout,
  and CSRF headers. Existing key login remains available.
- `patches/bundled-panel.patch`: `MANAGEMENT_STATIC_READONLY=true` prevents panel
  downloads and replacement, including when the file is missing.
- `patches/bazel.patch`: BUILD additions applied after Gazelle generates upstream
  package definitions. This build glue is separate from the upstream feature.

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
| `MANAGEMENT_OIDC_REDIRECT_URL`     | `https://<admin-host>/v0/management/callback`           |
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
foreign origins, and reject cross-site fetches. This also covers legacy management
GET operations that mutate state. Keep browser UI and management API on the same
origin. Explicit management keys continue to work without browser cookies.

## Updating upstream

Update each source revision and checksum, then rebase its feature patches. Refresh
`go.mod`/`go.sum` from the patched backend, retaining the build module name, and
refresh the isolated pnpm lock with Bazel-managed pnpm when frontend dependencies
change. Recheck `patches/bazel.patch` against Gazelle's generated BUILD files.
Run the test suite and image build above. When upstream releases contain the
features, update the source pins and remove the corresponding patches.
