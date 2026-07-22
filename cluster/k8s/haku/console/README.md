# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

## App-owned auth (the forward-auth outpost is retired)

The console authenticates its own surface instead of sitting behind the shared Authentik
proxy outpost. `httproute.yaml` points `haku.allegedly.works` straight at the console
Service; the retired `haku-dashboard` proxy provider and its deletion tombstone are gone.
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
console holds the Google OAuth clients and each Operator's refresh token itself. The console pod
starts fine before anything is connected; until an Operator connects, both servers are
`degraded` (hidden from that Operator) and their tools return a "connect your Google account"
error.

Authenticated-agent Calendar reads (`get_event`, `list_events`, `list_event_instances`) are
reviewed transparent auto-approved tools; `create_event` always remains operator-approved.

**Deploy prerequisites (operator, one time):**

1. **Gmail OAuth client secret.** The existing `haku-console-google-client-credentials` Secret
   (keys `client_id`, `client_secret`) supplies `HAKU_CONSOLE_GOOGLE_MAIL_CLIENT_{ID,SECRET}`.
   It is the restricted-scope Gmail project's client, independent of Airlock's
   `google-client-credentials`.
2. **Calendar OAuth client secret.** The separate `haku-console` Google Cloud project/client requests
   only `calendar.events`. Its client is stored in the SOPS-encrypted
   `haku-console-google-calendar-client-credentials` Secret with the same two keys. The deployment's
   references remain optional so a missing or temporarily unreconciled Secret degrades only Calendar.
3. **Redirect URI.** Register `https://haku.allegedly.works/api/provider-connections/callback`
   as an authorized redirect URI on both Google OAuth clients, or that client's callback fails with
   `redirect_uri_mismatch`.

**Connect (operator, per linkage):** open the console's Settings → Connected accounts and connect
Google Mail and Google Calendar separately, then complete consent (`access_type=offline`,
`prompt=consent`). Each callback stores its own refresh token in Postgres; disconnecting one linkage
deletes only that local grant. Separate projects/clients isolate their verification and credential
lifecycles.

**Gotcha — Testing publishing status expires the refresh token every 7 days.** The Gmail OAuth app
(project `rai-personal`) remains in **Testing** because its restricted scopes make publication require
the expensive verification/security-assessment path. Its connection therefore needs reauthorization
roughly weekly. Calendar uses a separate project/client so its narrower sensitive-scope verification
can proceed without Gmail's restricted scopes; once that project is published, Calendar tokens no
longer inherit Gmail's Testing-mode churn.

Scopes are explicit per deploy-named connection in `config.yaml`: Google Mail requests
`gmail.modify`, `gmail.compose`, and `gmail.settings.basic`; Google Calendar requests
`calendar.events`. Add a new logical connection when another Google surface is actually exposed
rather than broadening either existing grant.

## One-time bootstrap: `kubectl-passthrough-mcp` (cluster-admin, operator-linked)

The `kubectl-passthrough-mcp` MCP server entry (config.yaml — `pods_*`, `resources_*`,
`nodes_*`, `events_list`, `configuration_view`) uses `auth: {kind: remote_server_oauth}`, the same
per-operator browser-linked mechanism as `grocy-sf`: the operator connects once
from the console's Access tab (⚙ → Access → Connect next to `kubectl-passthrough-mcp`),
which runs Authentik's PKCE flow against `kubectl-passthrough-mcp`'s own OAuth2
application and stores the association in the console's Postgres database — no static
token, no secret to mount.

Unlike `grocy-sf`, this server forwards the connecting operator's own token
straight to kube-apiserver (`cluster_auth_mode = passthrough` in
`agents/kubectl-passthrough-mcp/`) rather than acting through a scoped service credential
of its own — the operator's real permissions apply, via the
`oidc-ksbx-agentydragon-admin` `ClusterRoleBinding`
(`agents/kubectl-passthrough-mcp/app/clusterrolebinding-agentydragon-admin.yaml`, cluster-admin).
So every tool call here runs with full cluster-admin once approved; the operator-approval
click in trusted console chrome is the only gate. See `haku/docs/security.md` for the
enforcement-inventory entry.

## Autonomous `haku_sandbox` MCP tools

The in-process `haku_sandbox` server is intentionally available to authenticated Agents without
approval. Its dedicated ServiceAccount and namespaced Role live in `../sandbox-mcp/`; the token is
projected at the standard in-cluster path only in the Python server container, never the nginx
sidecar. The code accepts only labeled claims for the configured `haku-bash` pool and exposes three
semantic operations (`reserve`, bounded `exec`, and `info`) instead of generic Kubernetes verbs.

The pool itself does not live in ducktape: haku-state owns the `SandboxTemplate` and
`SandboxWarmPool` through its constrained Flux reconciler. Follow the concrete checklist in
`haku/TODO.md` before expecting reserve to succeed. V1 has no startup hook; setup is an ordinary
first exec. Retained expired claims remain inspectable for seven days, after which
`haku-sandbox-claim-tombstones` removes them.

## Tana backend credential

`tana-rw` uses the cluster-internal Tana MCP endpoint with a static bearer held by the Console
server. The encrypted account PAT is reflected only into the `haku-console` namespace and injected
only into this deployment; the inner Haku workload sees the proxied tool surface, never the PAT.
The public Tana OAuth facade remains available for external MCP clients but is not on Haku's path.
