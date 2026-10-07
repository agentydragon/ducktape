# cluster/k8s/haku/console — Haku console deployment

Manifests for `haku/console/` (see that directory's README for the app itself). Deploy
notes here cover only what's specific to running it in-cluster.

The `console.k8s.yaml` and `kustomization.yaml` here are generated from
`cluster/cdk8s/haku/` (`charts.py` composes the database, migration, console and API proxy
in one Kustomization, emitted in the central Flux chart; `console_config.py` is the non-secret config
the `haku-console-config` ConfigMap carries, rendered and checked through the console's
own `Settings`). Regenerate per <../../../docs/cdk8s.md>. Hand-written beside them: the SOPS
Secrets, `image-pins/`, and the two ConfigMaps carrying Flux image markers
(`static-metadata.yaml`, `image-metadata.yaml`).

## App-owned auth (the forward-auth outpost is retired)

The console authenticates its own surface instead of sitting behind the shared Authentik
proxy outpost. The HTTPRoute points `haku.allegedly.works` at the standalone static
Service; its nginx proxies backend paths to the API Service. The retired `haku-dashboard`
proxy provider and its deletion tombstone are gone.
The operator browser-login provider is minted by `tf/gitops/agent-machine-access`; its credentials
and the session-signing secret are mirrored into `haku-console-oidc`. The former Console MCP OAuth
provider and `/mcp` protocol endpoint have been removed. nginx returns 404 for `/mcp` and its old
OAuth discovery paths, while operator browser OAuth remains under `/auth/*`.

**nginx ↔ app routing invariant.** The standalone `static` nginx Deployment serves
the SPA and proxies the app's top-level backend prefixes (`/api`, `/healthz`, and `/auth`; `/mcp` and its OAuth discovery paths return 404) to the `haku-console` API Service; everything else is
the SPA catch-all. So a **new top-level backend prefix needs a matching `location` in
`haku/console/default.conf.template`**, or it silently returns the SPA shell instead of reaching
the app (the footgun that first bit `/mcp`). The static Deployment's
`HAKU_CONSOLE_API_UPSTREAM` is derived from the API Service's name and port. nginx sets response headers (CSP/Cache-Control/…) only
on the static content it serves itself; the app owns the headers on everything proxied (`app.py`
`_security_headers`), so the two no longer write the same policy twice. The static Deployment has
no Console secrets, ServiceAccount token, or database access.

## Schema migrations are release work

`migration` is a fixed-name Job using the same Flux-selected `haku-console` image
as the API, running `server_bin migrate`; the command consumes only the database URL. The API
performs a zero-row ORM compatibility check at startup but never applies DDL.

**Nothing sequences the migration ahead of the API.** The database, the migration and the
console share one Flux Kustomization, and a Kustomization applies its objects without ordering
— so the API Deployment can be rolling while the migration Job is still running. Schema changes
must therefore be compatible with both the outgoing and incoming code (see _Rolling release
compatibility_ below, which the previous ordered-Kustomization layout already required). What
the ordering does still hold for is _dependent Kustomizations_: `wait` plus the database and
migration health checks keep anything with `dependsOn: haku-console` from reconciling until the
database is ready and the migration Job succeeds.

The Job has no Kubernetes API authority and is recreated only when its desired image or manifest
changes (`kustomize.toolkit.fluxcd.io/force: enabled`). It has no TTL, and it retries
(`backoffLimit: 10`, 20m deadline) because it has to wait out CNPG bootstrapping the Cluster
beside it. Each attempt leaves its own Pod, so a genuine migration failure is still readable
from the logs; it no longer fails after exactly one try. See `cluster/docs/troubleshooting.md` →
“A Failed Job Wedges Its Flux Kustomization”. It temporarily uses the existing CNPG
application-owner credential. The old `mcp_oauth_kv` table is retained through the rolling release;
remove it only after outgoing replicas no longer initialize it.

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

## Retired Console Recall integration

The Console reader, in-process Recall tool, indexer grant provisioner, and indexer credentials were
removed. Its Recall schema, data, and vector extension remain in Postgres through this rollout:
the outgoing API version still maps and validates the schema at startup. A follow-up migration can
drop them only after this retiring image has converged. The shared `haku/recall_index` library
remains used by Agentplane.

## `haku-console-github-mcp-client-credentials` belongs to agentplane-staging

The console reads nothing from this SOPS Secret, the pre-registered OAuth client of the GitHub App
behind GitHub's hosted MCP. `agentplane-staging` copies it with ESO, through a store that reaches
only this Secret, and its Action Service links GitHub with it
(<../../agentplane-staging/README.md> § MCP OAuth callbacks); deleting it here breaks that linkage.
Moving it there means re-encrypting it under that namespace, which needs the cluster decryption
identity (the SOPS MAC covers `metadata.namespace`). The same identity is needed to drop the file's
leftover Reflector annotations.
