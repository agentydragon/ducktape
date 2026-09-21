# Authentik Flux ownership handoff

Merge protection separately before folding `authentik-namespace`, `authentik-db`,
and `authentik-proxy-routes` into the surviving `authentik` Kustomization. The
application retains its existing Secrets, chart, database and proxy routes.
`authentik-db-backups` and `sso-providers-tf` remain separate owners.

## Cutover

1. Verify the three retiring live owners in `ducktape-flux` have `prune: false`
   and `deletionPolicy: Orphan`. Verify Ready status at the current generations
   where health status is reported. Do not infer live protection from merged Git.
2. Save local metadata-only baselines for all 29 direct resources, the Namespace,
   CNPG Cluster, database PVCs and volume bindings, and generated database Secrets.
   Confirm database, server and worker health. Keep detailed identifiers local.
3. Inspect generated Service selectors, Pod templates and existing Pod labels for
   Flux ownership labels, following the [operator traffic warning](../cluster/AGENTS.md#migrating-stateful-flux-kustomizations).
   Record ready EndpointSlice backends, Authentik readiness and OIDC discovery,
   and representative proxy-route behavior. Protection prevents deletion but does
   not alone prove traffic continuity.
4. Record the existing backup owner, its six direct resource identities, and
   metadata/controller references of its generated credentials Secret. Verify
   backup resource readiness and recent successful backup/WAL archiving. Backup
   resources, credentials and physical storage do not move in this change.
5. With consolidation CI green and the PR ready, suspend the three retiring
   owners and immediately re-read their protection fields. Keep `authentik`,
   `authentik-db-backups`, `sso-providers-tf`, and shared operators active.
6. Merge consolidation and wait for its artifact and reconciliation. Confirm the
   surviving inventory equals the union of the four old inventories: 29 direct
   objects, all owned by `authentik`. Confirm every direct object's identity and
   all database/PVC identities and volume bindings remain unchanged.
7. Verify PostgreSQL, HelmRelease, server and worker health; ready Service
   endpoints; Authentik readiness and OIDC discovery; representative proxy routes;
   and SSO Terraform reconciliation. Verify the backup owner and its resources,
   credential controller references, and working backup/WAL path are unchanged.
   Do not recreate state or rotate credentials to hurry adoption.

## Recovery

Prefer correcting reconciliation in place. A blind revert can shrink the surviving
owner's inventory while `prune: true` and delete transferred resources even with
`deletionPolicy: Orphan`. A reverse transfer first protects and suspends the current
owner, then restores old owners and verifies unchanged identities and traffic again.

## Scope

The consolidated root references only namespace, database, application and proxy
routes. Its artifact may carry the sibling backup and SSO directories, but they are
still applied exclusively by their existing separate owners. Backup ordering retains
CNPG/Barman and SeaweedFS prerequisites, without gating backups on the entire
Authentik application. SSO Terraform remains downstream of the running Authentik API.
