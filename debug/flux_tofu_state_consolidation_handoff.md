# Tofu state namespace ownership handoff

Merge protection before consolidation. The retiring `tofu-state-namespace` owns
the `tofu-state` Namespace; deleting that Namespace would also delete the shared
Terraform state database and its PVCs.

## Gate before consolidation

- Verify live `ducktape-flux/tofu-state-namespace` has `spec.prune: false` and
  `spec.deletionPolicy: Orphan`, with Ready observed at its current generation.
- Record the Namespace, CNPG Cluster `tofu-state-db-ovh`, PVC, and credential Secret
  UIDs and PVC volume bindings. Read Secret metadata only.
- Require both Flux owners Ready, CNPG instances Ready, PVCs Bound, and ready
  backends for the database's read/write Service.

The consolidation keeps `tofu-state-db` and its existing database and Secret,
broadens its artifact/path to include the Namespace, and removes the old namespace
owner and artifact. The CNPG admission prerequisite remains. No database resources
are renamed or recreated.

## Verify after consolidation

- Wait for `tofu-state-db` Ready at the new artifact revision and current generation.
- Confirm `tofu-state-namespace` is absent and the Namespace now carries the
  `tofu-state-db` Flux ownership label.
- Compare all captured UIDs and PVC bindings. Check database readiness and ready
  read/write Service EndpointSlices independently of Flux readiness.
- Confirm Terraform consumers still reconcile against the state backend.

Do not restore the old owner with pruning enabled during rollback: first restore it
with `prune: false` and `deletionPolicy: Orphan`. Protect the Namespace from pruning
before removing it from the surviving owner's rendered output.

Delete this note once the handoff and health checks are complete.
