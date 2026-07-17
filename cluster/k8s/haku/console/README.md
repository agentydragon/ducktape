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
backed by the console's own Postgres (`HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__KIND=postgres`,
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

The console's two Google-backed in-process MCP servers — `gmail` (`haku/console/tools/gmail.py` — Gmail
reads mirroring the REST API, draft creation, thread-label changes, label CRUD) and
`google_calendar` (`haku/console/tools/google_calendar.py` — recurrence-aware event reads and
creation), both behind the ordinary operator-approval queue — execute as the **acting
Operator's own Google account**: each call resolves that Operator's per-Operator Google access
token from the console's own connection store (`haku/console/provider_connection.py`),
self-refreshed in-process. This replaces Airlock's brokered `haku_console_google` token — the
console holds the Google OAuth client and each Operator's refresh token itself. The console pod
starts fine before anything is connected; until an Operator connects, both servers are
`degraded` (hidden from that Operator) and their tools return a "connect your Google account"
error.

Authenticated-agent Calendar reads (`get_event`, `list_events`, `list_event_instances`) are
reviewed transparent auto-approved tools; `create_event` always remains operator-approved.

**Deploy prerequisites (operator, one time):**

1. **Google OAuth client secret.** Author the `haku-console-google-client-credentials` Secret
   (keys `client_id`, `client_secret`) in the `haku-console` namespace with the shared Google
   OAuth client's credentials, `sops -e -i` it, and add it to `kustomization.yaml`. The
   deployment reads it via `HAKU_CONSOLE_GOOGLE_CLIENT__CLIENT_ID/SECRET` (`optional: true`, so
   the pod starts without it — both servers just stay degraded). Deliberately a console-owned
   Secret, independent of Airlock's `google-client-credentials` (no ESO/reflector coupling to
   the `airlock` namespace).
2. **Redirect URI.** Register `https://haku.allegedly.works/api/provider-connections/callback`
   as an authorized redirect URI on that Google OAuth client, or the callback fails with
   `redirect_uri_mismatch`.

**Connect (operator, per account):** open the console's Settings → Connected accounts and click
**Connect** on Google, then complete consent (`access_type=offline`, `prompt=consent`). The
callback stores the refresh token in the console's Postgres and self-refreshes the access token
thereafter; Disconnect revokes it.

**Gotcha — Testing publishing status expires the refresh token every 7 days.** The OAuth app
(project `rai-personal`) is in **Testing**, not **In production**, because the requested Gmail/Drive
scopes are _restricted_ and production verification would need a CASA security assessment. Google
expires **Testing-mode refresh tokens 7 days after issue**, so the connection breaks roughly weekly
and the Operator must re-**Connect** — the in-process self-refresh cannot save a refresh token Google
has already invalidated. To make tokens durable _without_ verification, flip the app to **In
production** and accept the unverified-app warning at consent (single-user, so the warning screen and
100-user cap are tolerable); only _removing_ that warning needs the CASA submission.

Scopes: `calendar.events`, `gmail.modify`, `gmail.compose`, `gmail.settings.basic`, plus every read-only scope the
`google` provider carries (`gmail.readonly`, `drive.readonly`, `drive.activity.readonly`,
`calendar.readonly`, `tasks.readonly`, `contacts.readonly`, `documents.readonly`,
`spreadsheets.readonly`, `presentations.readonly`, `youtube.readonly`) — requested in one grant
(`haku/console/provider_connection_registry.py`) so the console can read across the same surface
without a second consent round-trip. See `haku/docs/security.md` for the enforcement-inventory
entry.

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
