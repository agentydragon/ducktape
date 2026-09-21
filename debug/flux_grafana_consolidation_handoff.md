# Grafana Flux ownership handoff

Merge protection before folding `grafana-db` into the surviving `grafana-instance`
Kustomization. The shared monitoring Namespace, Grafana operator and shared Helm
repository keep their owners. The instance keeps its existing path and includes the
sibling database directory; its artifact copies exactly those two directories.

## Cutover

1. Verify live `ducktape-flux/grafana-db` is Ready at its current generation with
   `prune: false` and `deletionPolicy: Orphan`.
2. Capture fresh local metadata baselines for CNPG Cluster `monitoring/grafana-db-ovh`,
   its PVCs and volume bindings, the Grafana CR, Deployment and all directly managed
   resources. Record generated database Secret metadata only. Do not read the Grafana
   admin Secret. Record database/Deployment readiness and Grafana datasource/dashboard
   CR conditions, plus public application health.
3. Inspect relevant Service selectors, Pod templates and existing Pod labels for
   Flux ownership labels before transfer. Verify ready database and Grafana endpoints;
   object survival alone does not prove traffic continuity.
4. With consolidation CI green and the PR ready, suspend only the retiring
   `grafana-db` owner and re-read its protection fields immediately before merging.
   Keep `grafana-instance`, shared operators and the monitoring Namespace owner active.
5. Merge consolidation and wait for the broadened `grafana-instance` artifact and
   reconciliation. Do not delete the database, PVCs, generated Secrets or workloads.
6. Confirm the instance inventory is the exact union of both former inventories
   (21 objects). Verify the database ownership label becomes `grafana-instance`,
   while resource UIDs, PVC bindings and generated Secret controller references remain.
   Confirm database replicas, Grafana Deployment, ready endpoints, datasource/dashboard
   CR conditions and public application health. The existing instance objects retain
   their owner and configuration; no dashboard edits or admin credential access are
   needed for this check.

## Recovery

Prefer correcting reconciliation in place. Reverting the added database resource
while the survivor has `prune: true` can delete it; `deletionPolicy: Orphan` applies
when the owner itself is deleted, not when its rendered inventory shrinks. A reverse
transfer must first protect and suspend the survivor, then verify adoption by the
restored database owner with unchanged resource and volume identities.
