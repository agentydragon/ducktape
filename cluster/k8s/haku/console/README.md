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
`HAKU_CONSOLE_PUBLIC_BASE_URL` plus `/mcp`; it is not separately configurable. Root
`/.well-known/oauth-*` discovery points clients to those namespaced MCP OAuth endpoints, while
operator browser OAuth remains under `/auth/*` with its own provider and session.
The OIDCProxy's dynamic-client-registration + token state (shared across the two replicas) is
backed by the console's own Postgres (`HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__KIND=postgres`,
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

## haku-indexer — recall-index maintenance, separately deployed

`haku-indexer` (image `ghcr.io/agentydragon/haku-indexer` from `//haku/console:indexer_image`)
runs the two maintenance stages of `haku/console/recall_index_sync.py` as one binary in
role-flagged Deployments. The chunk role is one Deployment per logical index of the
`recall_indexes` registry: `indexer-chunk-<index>-deployment.yaml` runs `--role=chunk` mounting
only its own index's config slice (`indexer-chunk-<index>-config.yaml`, its own generated
ConfigMap), so `haku-indexer-chunk-haku-state`, `-haku-conversations`, and `-ducktape-public` each
sweep exactly their one index — the mounted config is the selection, with no selector env, and one
index's config change or parse breakage never touches another's pod. Each slice is the projection
of the registry in `config.yaml` (which the console still reads whole); the derivation tests
(`haku/console/test_deployment_config.py`, `cluster/validation/test_haku_manifest_contracts.py`)
pin every slice to its registry entry and the Deployment set to the registry, both ways. The embed
role stays one shared Deployment — `haku-indexer-embed` (`indexer-embed-deployment.yaml`,
`--role=embed`) — draining the shared embedding queue. The API pod keeps only the database readers
behind `haku_index.search`/`index_status`, so either role failing or rolling leaves search serving
the last committed index state, with staleness visible in `index_status`.

Replica counts are free on every side, by two mechanisms. Source sweeps: every logical index is
maintained under its per-index Postgres advisory lock, so replicas of an index's chunk pod — and
the other indexes' pods, and, during a rollout window, console replicas of a release that still
ran the whole-registry loop — only ever contend for a lock, never double-sync. The embedding
drain: each batch is claimed `FOR UPDATE SKIP LOCKED` for the draining transaction, so concurrent
embed replicas take disjoint batches, and conflict-safe vector insertion keeps even a claimless
legacy reader from publishing a conflicting vector.

Identity is deliberately narrow, and split to match the roles — and, for the chunk role, minimized
per index: each pod's config surface mirrors its credential mounts. No pod mounts a ServiceAccount
token or any of the console's operator/agent auth, approval-ledger, connector, or Web Push
credentials, and no chunk pod holds an embedder endpoint or another index's definition. Only the
`haku-state` chunk pod carries a Git credential — the `haku-forgejo-git` read slots its registry
entry names; the `haku-conversations` (chat) and `ducktape-public` (anonymous HTTPS) chunk pods
carry none. The embed pod holds the embedder endpoint and mounts nothing at all — not even a
config slice. Every pod shares exactly the `haku_indexer` database role. The role is declared
on the CNPG Cluster (`db/postgres-cluster.yaml` `managed.roles`; password from the ESO-generated
`haku-console-db-indexer` Secret). Its object grants are `indexer-role.sql` — recall-index tables
read/write plus `SELECT` on `conversation_item`, nothing else — applied by the
`haku-console-db-indexer-provisioner` Job in this app layer rather than `db/`, because the
`recall_index` schema exists only after the migration Job.

Schema compatibility follows the API's pattern at the worker's scope: startup does zero-row reads
of exactly the tables the role may touch and exits on failure, so an incompatible indexer image
crash-loops while the previous ReplicaSet keeps maintaining the index (`maxUnavailable: 0`). DDL
stays owned by the console-image migration Job above.

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

## One-time bootstrap: GitHub's hosted MCP server

GitHub's hosted MCP endpoint is `https://api.githubcopilot.com/mcp/`. It discovers its OAuth
authorization server normally, but GitHub does **not** support Dynamic Client Registration, so the
Console needs an organization-owned, pre-registered **GitHub App**. The Console uses GitHub's normal
endpoint: its upstream catalog includes write tools, but `config.yaml` explicitly auto-approves only
the reviewed read-only tool names for Haku. Every other GitHub tool remains per-call operator approval.

1. Create a private GitHub App owned by the organization. Set its user-authorization callback URL
   to `https://haku.allegedly.works/api/mcp/operator-auth/callback`. Grant only the repository and
   write permissions the intended toolset needs; Console approval never widens the App's GitHub
   permissions. Install/approve the App for the intended organization and
   repositories. Do not substitute a PAT or the OAuth client embedded in GitHub's local MCP binary.
2. Put the App's `client_id` and `client_secret` in a new SOPS-encrypted Secret named
   `haku-console-github-mcp-client-credentials`, with those exact keys. Add that manifest to this
   directory's `kustomization.yaml`. The Deployment already reflects the values as
   `HAKU_CONSOLE_GITHUB_MCP_CLIENT_{ID,SECRET}` and tolerates the Secret being absent until this
   step is complete.
3. Add the following server to `mcp.servers` in `config.yaml`, then reconcile the Console:

   ```yaml
   - id: github
     backend:
       kind: remote_mcp
       url: https://api.githubcopilot.com/mcp/
       auth:
         kind: remote_server_oauth
         scopes: [] # App permissions are the least-privilege boundary; do not request legacy broad scopes.
         client_registration:
           kind: preregistered
           client_id_env_var: HAKU_CONSOLE_GITHUB_MCP_CLIENT_ID
           client_secret_env_var: HAKU_CONSOLE_GITHUB_MCP_CLIENT_SECRET
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

## Colocated egress proxy (#4942, #4670)

The Console-authorized HTTP egress fence's proxy runs as the `egress-proxy` **sidecar** in this
Deployment (embedded mitmproxy adapter `haku/egress`, image `ghcr.io/agentydragon/haku-egress-proxy`),
gating every fenced-workload request against Console's decision endpoint. Colocation is #4670's ruled
topology; two guarantees are structural:

- **The oracle is loopback-bound (acceptance criterion 14).** `POST /api/internal/http/decide` serves
  on `127.0.0.1:8079` (`app._serve` / `build_internal_decide_app`), **not** on the network app
  `create_app` builds. No Service targets `:8079`, so only the in-pod sidecar reaches it over loopback
  — no sandbox has a route. This is load-bearing: the force-proxy CCNP admits `toEntities: cluster`, so
  a sandbox _can_ reach this pod's Service; the loopback bind, **not** a NetworkPolicy, is what keeps
  the oracle unreachable. The proxy-identity bearer authenticates every call (defense in depth).
- **Proxy and Console roll as one release unit.** One image commit ships both, so the decide call is an
  internal same-release contract needing no versioning. A Console roll severs in-flight tunnels — the
  accepted trade for not running a separately-versioned, separately-fenced proxy Deployment.

The fenced-workload-facing listener is `:8888` (`egress-proxy-service.yaml`,
`haku-egress-proxy.haku-console.svc`).

### Secret wiring

`config.yaml`'s `egress_decide` section names env vars the server resolves at startup
(`load_egress_decide`); absent, the endpoint stays `503` and the proxy fails closed.

- **`haku-egress-proxy-identity`** (`haku-egress-proxy-identity-eso.yaml`, one ESO Password generator
  into one Secret) — `fence-credential`, an opaque bearer **shared** by server and sidecar. The
  sidecar presents it in the `Authorization` header; the server authenticates only the shared fence,
  while the live bridge bearer identifies the sandbox Agent. It is compared in-memory and registered
  nowhere DB-side, so ESO regenerating it is safe — the Secret's Reloader annotation restarts the pod
  so both containers reload together. It is generated once (`refreshInterval: 8760h`).
- **Per-session bridge bearer** — the Console writes `HAKU_RUNNER_TOKEN` into each runtime
  `SandboxClaim.spec.env`. The runner uses that one bearer for its bridge, Console MCP, and any HTTP
  proxy selected in its launch environment, so HTTP egress and MCP resolve to the same live session
  Agent/profile/binding. This literal claim field is temporary: upstream Agent Sandbox does not yet
  support Secret-backed env injection. It is load-bearing that runtime `SandboxClaims` are not broadly
  readable and are not readable by agents; keep `get`/`list`/`watch` out of agent RBAC and review any
  new claim reader as access to a live bearer. The deferred Secret binding is tracked in
  `haku/sandbox/TODO.md`.
- **`haku-egress-github-token`** (ESO) — the `github-bot` registry credential (#4951). The Console holds
  the real value and substitutes it into decide responses; the sandbox only ever holds
  `github-token-placeholder`. Synced from the same `github-token` remote the retiring iron fence reads,
  via the claude-sandbox ClusterSecretStore (now admitting `haku-console`). Optional: an unset value is
  skipped with a warning (#4970), so a not-yet-synced Secret degrades the `github-bot` handle rather
  than crash-looping — the endpoint still serves reachability verdicts.
- **Shared interception CA** — the sidecar reuses `haku-egress-proxy-ca` (reflected into this
  namespace); an init container assembles it into `confdir/mitmproxy-ca.pem` so fenced sandboxes trust
  its leaves. A missing reflected Secret fails the init container and the pod never starts.

### The Cilium ceiling — ruled Option A

The sidecar shares the _trusted_ Console pod's netns, and Console needs broad cluster-internal egress,
so #4670's "public-only, deny private/cluster" network ceiling can't apply to this pod without breaking
it. The operator ruled **A — app-layer boundary**: no restrictive egress CCNP on the Console pod; the
destination boundary is the decide service's resolved-address-class validation (#4954
`_prohibited_address_class` + `egress_decide.prohibited_cidrs`), which rejects
loopback/link-local/multicast/private/RFC1918/ULA/configured-CIDR answers _before_ consulting grants. So
under colocation the app layer, not Cilium, denies private/cluster/metadata.

Recorded rejected alternatives: **B** (separate proxy Deployment with its own tight network ceiling)
reintroduces exactly what the ruling rejected — endpoint auth, an oracle NetworkPolicy, a versioned
contract — kept only as the fallback if data-plane load dominates. **C** (colocation plus an enumerated
Console egress CCNP) is the "true" ceiling but flips the trusted pod to default-deny egress and must
enumerate every Console destination; deferred until that set is pinned.

### Adoption, rollout, recovery

- **Repointed:** the force-proxy CCNP (`ccnp-haku-proxy-egress.yaml`) names the colocated listener, so
  fenced sandboxes can _reach_ `haku-console:8888`.
- **Rollout:** `push-images` builds `haku-egress-proxy` on the first `devel` merge; its ImagePolicy
  rewrites the placeholder tag. The Console rolls with the sidecar under `maxUnavailable: 0`.
- **Recovery:** the fence is fail-closed by construction — any decision-path error (server
  `503`/unreachable, timeout, malformed response, denied destination, address-class rejection) makes the
  proxy refuse, never forward. Recover by fixing the server (its logs show a `load_egress_decide` error
  or `HttpDecideUnavailableError`; the sidecar logs per-request `deny …: <reason>`, values never
  logged), not by bypassing it. A full-fidelity audit stream is a separate #4670 item.

### Deferred

The traffic **cutover** — repointing the Kyverno `inject-haku-egress-proxy` `HTTP_PROXY` from the
port-8080 iron fence to `haku-egress-proxy.haku-console.svc:8888` — is the adoption step, gated on the
first spike (#4943). The spike targets the public-coder-agent OpenClaw pod, which carries the
fence wiring itself (`../../agents/public-coder-agent/app/deployment.yaml`); the operator
procedure is <../../../../haku/egress/docs/github_spike.md>. Iron-fence retirement (which carries the shared CA out of
`cluster/k8s/agents/haku-egress-proxy/`), adoption of a live bridge bearer for every colocated
caller, Secret-backed binding for the per-session bearer (tracked in
`haku/sandbox/TODO.md`),
and the embedded runner's `stream_large_bodies` + h2/gRPC handling for broad adoption (dind layer pulls
OOM-killed the iron fence without the former; the sidecar is sized `1Gi` for the small-body spike until
then) are #4670 work items, not this PR.
