# External credential distribution

This is the cluster-wide pattern for static credentials for services operated
outside the cluster. The initial migrations are LLM provider keys, but the
pattern also applies to other eligible API credentials. Canonical SOPS Secret
manifests and the grants that authorize their consumers live in this
Kustomization. Most source Secrets live in `ducktape-flux`; a source may retain
an existing namespace when that name and namespace are an established consumer
contract.

Ownership follows the authorization boundary:

- The supplier owns the canonical Secret, an exact-name `get`-only Role, and a
  RoleBinding for each approved consumer ServiceAccount. These resources live
  together in `external-creds`; per-credential grant manifests use a
  `*-grants.yaml` suffix.
- `external-secrets-config` owns the shared `ClusterSecretStore`. Referent
  authentication resolves its `external-creds-reader` ServiceAccount in the
  consuming ExternalSecret's namespace; the store omits the ServiceAccount
  `namespace` field.
- The consumer owns its `external-creds-reader` ServiceAccount and
  ExternalSecret.
- The consumer Flux Kustomization depends on both `external-creds` and
  `external-secrets-config`. The supplier does not depend on ESO or consumer
  namespaces: a RoleBinding may name a ServiceAccount before that namespace or
  identity exists.

This keeps approval at the source. Referencing the shared ClusterSecretStore
does not grant access: the Kubernetes provider cannot read a canonical Secret
unless `external-creds` contains a RoleBinding for that namespace's
`external-creds-reader` ServiceAccount. The store's namespace conditions mirror
the approved namespaces as defense in depth. The approved namespace remains the
trust boundary: workloads or operators able to use its approved identity can
receive the credential.

Add a credential by creating one encrypted source Secret and its exact-name
Role. Add a consumer in two coordinated changes: add its supplier-owned
RoleBinding to the credential's grants manifest, then add the ServiceAccount,
ExternalSecret, and namespace to the shared store's conditions. A namespace
needs only one `external-creds-reader` ServiceAccount even when it receives
multiple approved credentials.

When moving existing live resources between Flux Kustomizations, follow Flux's
staged ownership-transfer procedure: disable pruning on the old owner, reconcile
the move and verify the new inventory, then restore pruning. Do not perform the
move in one reconciliation with pruning enabled.

This pattern excludes cluster-internal credentials, Kubernetes-native identity
and PKI material, and credentials minted or rotated by a controller. OAuth and
session credentials (for example Tana and CLIProxyAPI) remain explicit
exceptions. Normal workloads use scoped LiteLLM virtual keys instead of vendor
credentials where that is available.
