# Static frontend

`//agentplane/app/frontend/serving:static_image` packages the React bundle and service worker on
the pinned unprivileged nginx base. CI publishes it as `agentplane-frontend`, separately from the
`agentplane-app` Go API image. The API process runs as `1000:1000`, listens on port 8080, and accepts a
read-only root filesystem with one writable `/tmp` volume. `/healthz` reports local server health.

The Kubernetes Gateway shares one hostname between the two Services:

| Match                                                                       | Service    |
| --------------------------------------------------------------------------- | ---------- |
| Exact `/`, `/index.html`, `/main.js`, `/main.css`, `/sw.js`, `/favicon.svg` | `frontend` |
| All remaining paths, including auth and streams                             | `app`      |

The UI uses hash routing, so `/#/threads/...` loads the root document. Existing API URLs and OIDC
callbacks stay intact. nginx serves files only and has no API proxy, runtime credentials, Kubernetes
token or outbound network access. The API owns authentication, CSRF protection and Electric access.

The shell retains its fixed `/main.js` and `/main.css` names. All static responses use
`Cache-Control: no-store`; nginx disables ETags and modification-time conditional responses because
Bazel normalizes asset mtimes. Missing assets return 404 instead of the HTML shell. Content-addressed
URLs and immutable caching require a separate rollout design that keeps old and new asset versions
available while replicas roll and tabs remain open.

Run `bbr test //agentplane/app/frontend/serving:test_static_image` for the real image, read-only
filesystem, headers, asset types and missing-path behavior. App browser tests compose this same image
with the API behind their TLS HTTP/2 ingress, including root-scope service-worker registration.
