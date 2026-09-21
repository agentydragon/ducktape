# Agentplane upstream trust Bundle handoff

Staging and testing already have separate interception CA Secrets and runner trust
Bundles. Their proxy upstream trust accidentally shares the cluster-scoped
`agentplane-egress-upstream-ca` Bundle, whose namespace selector each Flux owner
overwrites. trust-manager removes the other environment's target ConfigMap;
replacement proxies fail to mount it. Reconciliation also triggers repeated rolls.

Keep the same trusted public roots and Kubernetes API CA. Give each environment its
own upstream Bundle and matching ConfigMap; no CA key rotation is involved.

1. Provision `agentplane-staging-egress-upstream-ca` and
   `agentplane-testing-egress-upstream-ca`, selecting only their own namespaces.
   Retain the old Bundle and proxy mounts in this preparation change. Verify each
   new Bundle's Synced condition observes its current generation and its ConfigMap
   exists in the intended namespace. The old collision can still prevent an
   environment's overall Flux Ready condition; that is not the preparation gate.
2. Once both new ConfigMaps exist, switch the proxy volume references and remove
   the old Bundle from both charts. Either Flux owner can prune the shared Bundle
   and its derived ConfigMaps without touching the new ones. An old replica may
   lose its old ConfigMap during the rollout; replacement replicas mount the
   already-provisioned, independently owned ConfigMaps. No authored data, Secrets,
   databases, or volumes are removed.
3. Verify staging has two updated ready proxy replicas and testing has one, every
   Pod mounts its own upstream ConfigMap, both Flux owners are Ready, and the old
   Bundle is absent. Check ready service endpoints and authenticated egress in
   both environments; readiness alone does not prove upstream authentication.

After step 2, restore the old Bundle before reverting mounts if rollback is needed.
