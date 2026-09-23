# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

The `haku-console.k8s.yaml` and `kustomization.yaml` here are generated from
`cluster/cdk8s/haku/` (`charts.py` composes the database, migration, console and API proxy
in one Kustomization, emitted in the central Flux chart; `console_config.py` is the non-secret config
the `haku-console-config` ConfigMap carries, rendered and checked through the console's
own `Settings`). Regenerate per <../../../docs/cdk8s.md>. Hand-written beside them: the SOPS
Secrets, `indexer-role.sql` (a `configMapGenerator` input, so a changed script re-hashes
the ConfigMap and recreates the provisioner Job), `image-pins/`, and the two ConfigMaps
carrying Flux image markers (`static-metadata.yaml`, `image-metadata.yaml`).

## App-owned auth (the forward-auth outpost is retired)

The console authenticates its own surface instead of sitting behind the shared Authentik
proxy outpost. The HTTPRoute points `haku.allegedly.works` at the standalone static
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
`HAKU_CONSOLE_API_UPSTREAM` is derived from the API Service's name and port. nginx sets response headers (CSP/Cache-Control/…) only
on the static content it serves itself; the app owns the headers on everything proxied (`app.py`
`_security_headers`), so the two no longer write the same policy twice. The static Deployment has
no Console secrets, ServiceAccount token, or database access.

## Schema migrations are release work

`haku-console-migration` is a fixed-name Job using the same Flux-selected `haku-console` image
as the API, running `server_bin migrate`; the command consumes only the database URL. The API
performs a zero-row ORM compatibility check at startup but never applies DDL.

**Nothing sequences the migration ahead of the API.** The database, the migration and the
console share one Flux Kustomization, and a Kustomization applies its objects without ordering
— so the API Deployment can be rolling while the migration Job is still running. Schema changes
must therefore be compatible with both the outgoing and incoming code (see _Rolling release
compatibility_ below, which the previous ordered-Kustomization layout already required). What
the ordering does still hold for is _dependent Kustomizations_: `wait` plus the Job health
checks keep anything with `dependsOn: haku-console` from reconciling until both Jobs succeed.

The Job has no Kubernetes API authority and is recreated only when its desired image or manifest
changes (`kustomize.toolkit.fluxcd.io/force: enabled`). It has no TTL, and it retries
(`backoffLimit: 10`, 20m deadline) because it has to wait out CNPG bootstrapping the Cluster
beside it. Each attempt leaves its own Pod, so a genuine migration failure is still readable
from the logs; it no longer fails after exactly one try. See `cluster/docs/troubleshooting.md` →
“A Failed Job Wedges Its Flux Kustomization”. It temporarily uses the existing CNPG
application-owner credential; splitting DDL ownership from runtime DML must first migrate the
externally managed `mcp_oauth_kv` table and make a deliberate ownership/grant handoff for the
live database.

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

The migration Job runs alongside the new API workload, while previous API replicas may still be
serving, so a release's schema has to satisfy the old code, the new code, and the window where
the migration has not finished. Database changes therefore use expand/contract. Dropping or renaming an ORM-mapped column
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

## One-time bootstrap: GitHub's hosted MCP server

GitHub's hosted MCP endpoint is `https://api.githubcopilot.com/mcp/`. It discovers its OAuth
authorization server normally, but GitHub does **not** support Dynamic Client Registration, so the
Console needs an organization-owned, pre-registered **GitHub App**. The Console uses GitHub's normal
endpoint: its upstream catalog includes write tools, but `console_config.py` explicitly auto-approves only
the reviewed read-only tool names for Haku. The same entry denies the Copilot delegation tools to every Agent, including stale-schema and generic-dispatch calls. Other GitHub tools remain per-call operator approval.

1. Create a private GitHub App owned by the organization. Set its user-authorization callback URL
   to `https://haku.allegedly.works/api/mcp/operator-auth/callback`. Grant only the repository and
   write permissions the intended toolset needs; Console approval never widens the App's GitHub
   permissions. Install/approve the App for the intended organization and
   repositories. Do not substitute a PAT or the OAuth client embedded in GitHub's local MCP binary.
2. Put the App's `client_id` and `client_secret` in a new SOPS-encrypted Secret named
   `haku-console-github-mcp-client-credentials`, with those exact keys, listed in the `haku-console`
   directory's `extra_resources` (`cluster/cdk8s/haku/charts.py`). The Deployment overlays the values directly at
   `HAKU_CONSOLE__MCP__SERVERS__GITHUB__BACKEND__AUTH__CLIENT_REGISTRATION__CLIENT_{ID,SECRET}`
   and tolerates the Secret being absent until this step is complete.
3. Keep the existing keyed `mcp.servers.github` entry in `console_config.py`. Its non-secret shape (as YAML) is:

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
