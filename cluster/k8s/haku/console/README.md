# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

## App-owned auth (the forward-auth outpost is retired)

The console authenticates its own surface instead of sitting behind the shared Authentik
proxy outpost. `httproute.yaml` points `haku.allegedly.works` at the standalone static
Service; its nginx proxies backend paths to the API Service. The retired `haku-dashboard`
proxy provider and its deletion tombstone are gone.
Two Authentik OAuth2 providers, minted by `tf/gitops/agent-machine-access` (application slugs
`haku-console` for operator browser login and `haku-console-mcp` for the `/mcp` OIDCProxy
upstream), write their client secrets + the operator session-signing secret into the
`haku-console-oidc` Secret; single-user access is Authentik's application access policy.
The MCP issuer/callback URL is derived from the console's canonical
`HAKU_CONSOLE__PUBLIC_BASE_URL` plus `/mcp`; it is not separately configurable. Root
`/.well-known/oauth-*` discovery points clients to those namespaced MCP OAuth endpoints, while
operator browser OAuth remains under `/auth/*` with its own provider and session.
The OIDCProxy's dynamic-client-registration + token state (shared across the two replicas) is
backed by the console's own Postgres (`HAKU_CONSOLE__MCP_OAUTH__PERSISTENCE__KIND=postgres`,
py-key-value's `PostgreSQLStore` auto-creating a `mcp_oauth_kv` table) — no separate valkey, unlike
the grocy/tana MCP facades. Deviation from those facades: the operator browser login is the console's
own app-native OIDC (not an outpost and not a separate SPA), so a 401 from `/api/*` bounces the
browser to `/auth/login`.

**nginx ↔ app routing invariant.** The standalone `haku-console-static` nginx Deployment serves
the SPA and proxies the app's top-level backend prefixes (`/api`, `/healthz`, `/mcp`, `/auth`, and
the `/.well-known/oauth-*` discovery docs) to the `haku-console` API Service; everything else is
the SPA catch-all. So a **new top-level backend prefix needs a matching `location` in
`haku/console/default.conf.template`**, or it silently returns the SPA shell instead of reaching
the app (the footgun that first bit `/mcp`). The static Deployment's
`HAKU_CONSOLE_API_UPSTREAM` is the API Service DNS name plus port; keep it aligned with
`service.yaml`. nginx sets response headers (CSP/Cache-Control/…) only
on the static content it serves itself; the app owns the headers on everything proxied (`app.py`
`_security_headers`), so the two no longer write the same policy twice. The static Deployment has
no Console secrets, ServiceAccount token, or database access.

## Schema migrations are release work

`haku-console-migration` is a fixed-name Job run by its own Flux Kustomization before the
Console workloads reconcile. It uses the same Flux-selected `haku-console` image as the API and
runs `server_bin migrate`; the command consumes only the database URL. The API performs a
zero-row ORM compatibility check at startup but never applies DDL. This keeps a migration failure
from replacing serving API replicas.

The Job has no Kubernetes API authority and is recreated only when its desired image or manifest
changes (`kustomize.toolkit.fluxcd.io/force: enabled`). It intentionally has neither a TTL nor an
automatic retry loop: a failed release remains inspectable and blocks its dependent workload until
an operator deletes `haku-console-migration` and reconciles `haku-console-migration` in
`ducktape-flux`. See `cluster/docs/troubleshooting.md` → “A Failed Job Wedges Its Flux
Kustomization”. It temporarily uses the existing CNPG application-owner credential; splitting
DDL ownership from runtime DML must first migrate the externally managed `mcp_oauth_kv` table and
make a deliberate ownership/grant handoff for the live database.

## Rolling release compatibility

The API and static shell are separate Deployments and roll independently. The API uses
`maxUnavailable: 0`: a replacement that never becomes Ready leaves the running version serving,
but old and new replicas overlap while a healthy release converges.

Each static image contains only its own fingerprinted assets. During a static roll, a browser can
load a shell from one replica and request its chunk from another replica that does not have it. The
window lasts only for the roll and a refresh afterwards repairs the page. Session persistence could
close the gap, but Service `sessionAffinity` is not known to survive the Cilium Gateway API/Envoy
path; verify that path before configuring it. Until then, API/static compatibility must remain
additive across their independent rolls.

The migration Job runs before the new API workload, while previous API replicas may still be
serving. Database changes therefore use expand/contract. Dropping or renaming an ORM-mapped column
requires three releases: add the replacement, stop mapping the old column, then drop it only after
the unmapping release has converged. SQLAlchemy names every mapped column in ordinary model
`SELECT`s even when application code does not read the attribute. `database_schema.py` records
unmapped tables, columns, and indexes waiting for their final drop so schema drift checks preserve
that release boundary.

Stored values and cross-replica payloads have the analogous adjacent-release rule; the reader/writer
vocabulary policy remains in <../../../../haku/console/README.md> § Vocabularies across a roll.

## Recall indexing is currently unwired

The deployed console does not register the `haku_index` MCP server, grant Recall indexes to any
access profile, or run source/embedding maintenance workers. This stops indexing and keeps retained
indexes out of connected MCP-client catalogs. The `recall_index` schema/data and the narrow
`haku_indexer` database role remain in place for now; no migration drops them. Re-enabling Recall
must restore the catalog, access-profile grants, and maintenance workers as one reviewed change.

## One-time bootstrap: the in-process `gmail` + `google_calendar` MCP servers

The console's two Google-backed in-process MCP servers — `gmail` (`haku/console/tools/gmail.py` — Gmail
reads mirroring the REST API, draft creation, thread-label changes, label CRUD) and
`google_calendar` (`haku/console/tools/google_calendar.py` — recurrence-aware event reads and
creation), both behind the ordinary operator-approval queue — execute as the **acting
Operator's own Google account**: each call resolves that Operator's per-Operator Google access
token from the console's own connection store (`haku/console/oauth/provider_connection.py`),
self-refreshed in-process. This replaces Airlock's brokered `haku_console_google` token — the
console holds the Google OAuth clients and each Operator's refresh token itself. The console pod
starts fine before anything is connected; until an Operator connects, both servers are
`degraded` (hidden from that Operator) and their tools return a "connect your Google account"
error.

Authenticated-agent Calendar reads (`get_event`, `list_events`, `list_event_instances`) are
reviewed transparent auto-approved tools; `create_event` always remains operator-approved.

**Deploy prerequisites (operator, one time):**

1. **Gmail OAuth client secret.** The existing `haku-console-google-client-credentials` Secret
   (keys `client_id`, `client_secret`) supplies the nested
   `HAKU_CONSOLE__OPERATOR_CONNECTION_PROVIDERS__GOOGLE_MAIL__CLIENT_{ID,SECRET}` settings.
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

## One-time bootstrap: GitHub's hosted MCP server

GitHub's hosted MCP endpoint is `https://api.githubcopilot.com/mcp/`. It discovers its OAuth
authorization server normally, but GitHub does **not** support Dynamic Client Registration, so the
Console needs an organization-owned, pre-registered **GitHub App**. The Console uses GitHub's normal
endpoint: its upstream catalog includes write tools, but `config.yaml` explicitly auto-approves only
the reviewed read-only tool names for Haku. The same entry denies the Copilot delegation tools to every Agent, including stale-schema and generic-dispatch calls. Other GitHub tools remain per-call operator approval.

1. Create a private GitHub App owned by the organization. Set its user-authorization callback URL
   to `https://haku.allegedly.works/api/mcp/operator-auth/callback`. Grant only the repository and
   write permissions the intended toolset needs; Console approval never widens the App's GitHub
   permissions. Install/approve the App for the intended organization and
   repositories. Do not substitute a PAT or the OAuth client embedded in GitHub's local MCP binary.
2. Put the App's `client_id` and `client_secret` in a new SOPS-encrypted Secret named
   `haku-console-github-mcp-client-credentials`, with those exact keys. Add that manifest to this
   directory's `kustomization.yaml`. The Deployment overlays the values directly at
   `HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_{ID,SECRET}`
   and tolerates the Secret being absent until this step is complete.
3. Keep the existing keyed `mcp.servers.github` entry in `config.yaml`. Its non-secret shape is:

   ```yaml
   github:
     id: github
     backend:
       kind: remote_mcp
       url: https://api.githubcopilot.com/mcp/
       auth:
         kind: remote_server_oauth
         client_registration:
           kind: preregistered
           token_endpoint_auth_method: client_secret_post
   ```

4. In Console Settings → Access, connect the GitHub server and complete GitHub's authorization
   prompt. The Console stores each operator's grant separately; disconnecting replaces only that
   operator's link. Haku's reviewed reads execute immediately; GitHub writes always enter the
   Console's per-call approval queue.

GitHub's host guide describes the prerequisite and explicitly notes that its remote MCP server has
no Dynamic Client Registration: <https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md>.

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

## Tana backend credential

`tana-rw` uses the cluster-internal Tana MCP endpoint with a static bearer held by the Console
server. The encrypted account PAT is reflected only into the `haku-console` namespace and injected
only into this deployment; the inner Haku workload sees the proxied tool surface, never the PAT.
The public Tana OAuth facade remains available for external MCP clients but is not on Haku's path.
