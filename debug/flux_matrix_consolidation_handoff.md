# Matrix Flux ownership handoff

Merge protection before folding `matrix-namespace` and `matrix-db` into `matrix`.
The surviving owner composes namespace, database and app manifests; the API-driven
`matrix-user-provisioner` stays separate and downstream of the healthy HelmRelease.

## Cutover

1. Verify both retiring live owners in `ducktape-flux` are Ready at their current
   generations with `prune: false` and `deletionPolicy: Orphan`.
2. Capture a fresh local baseline of the Namespace, CNPG Cluster, database PVCs,
   HelmRelease and Helm-created media PVC: UIDs, volume bindings, database health,
   Helm release identity, workload readiness and ready Service endpoints. Keep
   Secret evidence metadata-only and local. Preserve all existing encrypted keys,
   passwords and registration credentials.
3. Inspect Service selectors, workload Pod templates and existing Pod labels for
   Flux ownership labels. The initial read-only check found only stable application,
   component and CNPG selectors; chart Redis is a Deployment, not an operator CR.
   Recheck at cutover. Orphan protection does not guarantee traffic continuity.
4. Once consolidation CI is green and the PR is ready, suspend `matrix-namespace`
   and `matrix-db`, then re-read their protection fields immediately before merge.
   Keep `matrix`, shared operators and `matrix-user-provisioner` active.
5. Merge consolidation and wait for the broadened `matrix-app` artifact and
   `matrix` reconciliation. Never delete workloads, Secrets or storage to hurry it.
6. Confirm the surviving inventory is the exact union of the three old inventories,
   with each direct object owned by `matrix`. Verify unchanged Namespace, CNPG,
   HelmRelease and PVC identities/bindings, unchanged Helm release name, healthy
   database and Synapse/Element/Redis workloads, and ready Service endpoints.
7. Check the public Matrix client versions endpoint and Element page, plus Synapse's
   ability to use its database and Redis. Check the separate user provisioner remains
   healthy; do not rerun registration or rotate credentials to prove adoption.

The Helm release keeps its existing name, chart version, resource configuration and
media storage. Continuous install/upgrade retries let missing OIDC credentials or
runtime services converge after removing readiness gates. The disabled signing-key
hook stays disabled; the existing signing key is retained.

## Recovery

Prefer correcting reconciliation in place. A blind revert can shrink the surviving
owner's inventory and prune the adopted Namespace/database even with
`deletionPolicy: Orphan`. Reverse transfer requires protecting and suspending the
current owner first, then verifying adoption by restored owners with the original
resource and volume identities.

The initial preflight found PostgreSQL 2/2, all three PVCs Bound, and
Synapse/Element/Redis and the HelmRelease ready. Refresh those observations before
cutover; this runbook does not authorize a live mutation by itself.
