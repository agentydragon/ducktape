# Gatus Flux ownership handoff

Merge protection separately before consolidating `gatus-namespace` and `gatus-db`
into the surviving `gatus` Kustomization. Namespace, database and application become
one owner; `gatus-sso-tf` remains a separate Authentik API provisioning stage.

## Cutover

1. Verify both retiring live owners in `ducktape-flux` have `prune: false`,
   `deletionPolicy: Orphan`, and Ready status at their current generations.
2. Save local metadata-only baselines for every direct resource, the Namespace,
   CNPG Cluster, database PVCs and volume bindings, and generated database Secrets.
   Confirm the database and application are healthy. Keep detailed identifiers local.
3. Inspect generated database/application Service selectors, Pod templates and existing
   Pod labels. Follow the [operator label warning](../cluster/AGENTS.md#migrating-stateful-flux-kustomizations)
   if any selector contains Flux ownership labels; orphan protection alone does not
   prevent traffic loss. Record ready EndpointSlice backends and application health.
4. Once consolidation CI is green and the PR is ready, suspend `gatus-namespace` and
   `gatus-db` and immediately re-read their protection fields. Keep `gatus`, shared
   operators, and `gatus-sso-tf` active. Merge consolidation only after these checks.
5. Wait for the new artifact and reconciliation. Verify the surviving inventory is
   exactly the union of the three previous inventories: ten direct resources, all
   owned by `gatus`. Confirm unchanged direct-resource and database/PVC identities,
   volume bindings and generated Secret controller references. Do not recreate data
   or credentials to hurry adoption.
6. Verify healthy PostgreSQL, HelmRelease and Deployment; ready Service endpoints;
   Gatus HTTP health from a consumer; and successful SSO Terraform reconciliation.
   Gatus probes may report unrelated application failures: distinguish its own
   availability from the applications it monitors.

## Recovery

Prefer correcting reconciliation in place. A blind revert can shrink the surviving
owner's inventory while `prune: true` and delete transferred objects even with
`deletionPolicy: Orphan`. A reverse handoff requires protecting and suspending the
current owner before restoring old owners, then verifying unchanged identities and
service continuity again.
