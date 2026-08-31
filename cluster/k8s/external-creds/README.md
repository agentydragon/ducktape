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
- The consumer owns its ServiceAccount, namespace-local SecretStore, and
  ExternalSecret. A namespaced SecretStore authenticates as a ServiceAccount in
  its own namespace; omit the ServiceAccount `namespace` field.
- The consumer Flux Kustomization depends on both `external-creds` and
  `external-secrets-config`. The supplier does not depend on ESO or consumer
  namespaces: a RoleBinding may name a ServiceAccount before that namespace or
  identity exists.

This keeps approval at the source. A namespace can create an ExternalSecret or
SecretStore, but the Kubernetes provider cannot read a canonical Secret unless
`external-creds` contains a RoleBinding for that namespace and ServiceAccount.
The approved namespace remains the trust boundary: workloads or operators able
to use that namespace's approved store can receive the credential.

Add a credential by creating one encrypted source Secret and its exact-name
Role. Add a consumer in two coordinated changes: add its supplier-owned
RoleBinding to the credential's grants manifest, then add the ServiceAccount,
SecretStore, and ExternalSecret to the consumer. Do not use a shared
ClusterSecretStore for these credentials: it would broaden the receiving
namespace set and weaken the source-controlled capability boundary.

When moving existing live resources between Flux Kustomizations, follow Flux's
staged ownership-transfer procedure: disable pruning on the old owner, reconcile
the move and verify the new inventory, then restore pruning. Do not perform the
move in one reconciliation with pruning enabled.

This pattern excludes cluster-internal credentials, Kubernetes-native identity
and PKI material, and credentials minted or rotated by a controller. OAuth and
session credentials (for example Tana and CLIProxyAPI) remain explicit
exceptions. Normal workloads use scoped LiteLLM virtual keys instead of vendor
credentials where that is available.
