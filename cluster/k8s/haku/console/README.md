# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

## App-owned auth (the forward-auth outpost is retired)

The console authenticates its own surface instead of sitting behind the shared Authentik
proxy outpost. `httproute.yaml` points `haku.allegedly.works` straight at the console
Service; the `haku-dashboard` proxy provider is tombstoned in
`cluster/k8s/authentik/app/blueprints/haku-dashboard-sso.yaml` (delete the tombstone after a
few reconcile cycles, once it's gone from Authentik) and removed from the embedded outpost.
Two Authentik OAuth2 providers, minted by `tf/gitops/agent-machine-access` (application slugs
`haku-console` for operator browser login and `haku-console-mcp` for the `/mcp` OIDCProxy
upstream), write their client secrets + the operator session-signing secret into the
`haku-console-oidc` Secret; single-user access is Authentik's application access policy.
The MCP issuer/callback URL is derived from the console's canonical
`HAKU_CONSOLE_PUBLIC_BASE_URL` plus `/mcp`; it is not separately configurable. Root
`/.well-known/oauth-*` discovery points clients to those namespaced MCP OAuth endpoints, while
operator browser OAuth remains under `/auth/*` with its own provider and session.
The OIDCProxy's dynamic-client-registration + token state (shared across the two replicas) is
backed by the console's own Postgres (`HAKU_CONSOLE_MCP_OAUTH_PERSISTENCE__KIND=postgres`,
py-key-value's `PostgreSQLStore` auto-creating a `mcp_oauth_kv` table) — no separate valkey, unlike
the grocy/tana MCP facades. Deviation from those facades: the operator browser login is the console's
own app-native OIDC (not an outpost and not a separate SPA), so a 401 from `/api/*` bounces the
browser to `/auth/login`.

**nginx ↔ app routing invariant.** The `haku-console-static` nginx sidecar serves the SPA and
proxies the app's top-level backend prefixes (`/api`, `/healthz`, `/mcp`, `/auth`, and the
`/.well-known/oauth-*` discovery docs) to FastAPI; everything else is the SPA catch-all. So a **new
top-level backend prefix needs a matching `location` in `haku/console/default.conf.template`**, or
it silently returns the SPA shell instead of reaching the app (the footgun that first bit `/mcp`).
nginx sets response headers (CSP/Cache-Control/…) only on the static content it serves itself; the
app owns the headers on everything proxied (`app.py` `_security_headers`), so the two no longer
write the same policy twice.

## One-time bootstrap: the in-process `gmail` + `google_calendar` MCP servers

The console's two in-process MCP servers — `gmail` (`haku/console/tools/gmail.py` — Gmail
reads mirroring the REST API, draft creation, thread-label changes, label CRUD) and
`google_calendar` (`haku/console/tools/google_calendar.py` — calendar event creation), both behind the
ordinary operator-approval queue — are both built from the single `haku_console_google` Airlock
token and need a one-time browser consent for its provider before they're functional. The
console pod itself starts fine either way (the token volume is `optional: true`); until consent
happens, those servers' tools error on invocation instead of running.

0. No Google console change needed: `haku_console_google` reuses the `google` provider's
   already-registered redirect URI (`…/oauth/callback/google`) on the same OAuth client —
   the callback resolves the provider from OAuth `state`, not the path.
1. Visit `https://airlock.allegedly.works/oauth/authorize/haku_console_google` and consent
   as the target Google account. Airlock's callback writes `haku-console-google-tokens`
   (refresh) and `haku-console-google-access-token` (access-only) into the `airlock`
   namespace; the refresh loop keeps the access token fresh thereafter.
2. ESO mirrors `haku-console-google-access-token` into the `haku-console` namespace within
   ~1m. No restart needed — the console re-reads the mounted token via google-auth's
   `refresh_handler` on each rotation.

Provider config: `agents/airlock/config.yaml` (`haku_console_google`); the same Google
OAuth client as `google` is reused via
`HAKU_CONSOLE_GOOGLE_CLIENT_ID/SECRET` (`agents/airlock/deployment.yaml`).

Scopes: `calendar.events`, `gmail.modify`, `gmail.compose`, plus every read-only scope the
`google` provider carries (`gmail.readonly`, `drive.readonly`, `drive.activity.readonly`,
`calendar.readonly`, `tasks.readonly`, `contacts.readonly`, `documents.readonly`,
`spreadsheets.readonly`, `presentations.readonly`, `youtube.readonly`) — kept in one grant
since the console's `gmail` and `google_calendar` servers both consume it, and carrying the
read scopes means a future haku-console read feature outside Gmail (Drive/Docs/…) needs no
second consent round-trip. Deliberately its own provider (not reusing `google`) so no other
consumer's token is ever upgraded to this scope set. See
`haku/docs/security.md` for the enforcement-inventory entry.

## One-time bootstrap: `kubectl-passthrough-mcp` (cluster-admin, operator-linked)

The `kubectl-passthrough-mcp` MCP server entry (config.yaml — `pods_*`, `resources_*`,
`nodes_*`, `events_list`, `configuration_view`) uses `operator_oauth`, the same
per-operator browser-linked mechanism as `grocy-sf`/`tana-rw`: the operator connects once
from the console's Access tab (⚙ → Access → Connect next to `kubectl-passthrough-mcp`),
which runs Authentik's PKCE flow against `kubectl-passthrough-mcp`'s own OAuth2
application and stores the association in the console's Postgres database — no static
token, no secret to mount.

Unlike `grocy-sf`/`tana-rw`, this server forwards the connecting operator's own token
straight to kube-apiserver (`cluster_auth_mode = passthrough` in
`agents/kubectl-passthrough-mcp/`) rather than acting through a scoped service credential
of its own — the operator's real permissions apply, via the
`oidc-ksbx-agentydragon-admin` `ClusterRoleBinding`
(`agents/kubectl-passthrough-mcp/app/clusterrolebinding-agentydragon-admin.yaml`, cluster-admin).
So every tool call here runs with full cluster-admin once approved; the operator-approval
click in trusted console chrome is the only gate. See `haku/docs/security.md` for the
enforcement-inventory entry.
