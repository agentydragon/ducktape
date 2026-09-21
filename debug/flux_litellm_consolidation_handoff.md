# LiteLLM Flux ownership handoff

Merge protection separately before consolidating `litellm-namespace`, `litellm-db`
and `litellm-secrets` into the surviving `litellm` Kustomization. All four current
inventories become one; `litellm-keys-tf` remains downstream because it provisions
keys through the running API. Including it would restore a bootstrap cycle.

## Cutover

1. Verify the three retiring **live** owners in `ducktape-flux` have `prune: false`,
   `deletionPolicy: Orphan`, and Ready status at their current generations.
2. Refresh the Namespace, CNPG Cluster and PVC UID/volume-binding baseline below.
   Confirm PostgreSQL and both application replicas are healthy. Record the UIDs and
   controller references of ExternalSecrets and their generated Secrets using
   metadata-only reads; preserve the existing encrypted master and salt keys.
3. With consolidation CI green and the PR ready, suspend those three owners and
   re-read their protection fields immediately before merging. Keep the surviving
   app, shared operators, and `litellm-keys-tf` active.
4. Merge consolidation, then wait for its artifact and reconciliation. Old owners
   are removed with Orphan semantics; do not delete workloads, volumes, or Secrets
   to hurry adoption.
5. Verify the surviving Flux inventory is the exact union of the four old inventories
   (17 resources), and every direct object's ownership label is `litellm`. Confirm
   unchanged Namespace/Cluster/PVC UIDs and volume bindings; unchanged ExternalSecret
   identities and generated Secret controller references; and healthy database,
   Deployment, external readiness endpoint, and downstream key provisioning.
6. Check all relevant Services still select ready endpoints. Langfuse's preceding
   handoff showed that an operator can copy Flux ownership labels into Service
   selectors before existing Pods update. LiteLLM's current app/DB selectors do not
   use those labels; verify this remains true at cutover rather than inferring
   application health from Flux Ready alone.

Gatus's dependency on the retired `litellm-secrets` owner is removed without replacing
it with a whole-LiteLLM health gate. Its reflected key reference remains unchanged.
No credential rotation, database changes, model configuration changes, or API-key
provisioning refactor belongs in this ownership move.

## Recovery

Prefer correcting the surviving owner's reconciliation in place. A blind revert can
shrink its inventory while `prune: true`, deleting the moved resources even with
`deletionPolicy: Orphan`. A reverse transfer requires protecting and suspending the
current owner first, then verifying adoption by the restored owners with the same
resource identities.

## Pre-cutover evidence

Keep the detailed UID, volume-binding and Secret-controller-reference snapshots local
to the operator performing cutover. The initial read-only check found PostgreSQL and
the Deployment both at 2/2 ready, two Bound database PVCs, and all ExternalSecrets
synced. Refresh this evidence immediately before cutover.

ESO copies Flux labels onto generated Secrets, so those labels may change during
adoption. Their UIDs and controller references must remain; do not add generated
Secrets to the consolidated Flux inventory.
