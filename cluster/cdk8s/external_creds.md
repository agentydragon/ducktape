# External credential distribution

This is the cluster-wide pattern for static credentials used by services outside
the cluster. It began with LLM provider keys and also applies to other eligible
API credentials. The canonical SOPS Secret manifests and generated grants are
assembled by the [`cluster/k8s/external-creds`](../k8s/external-creds)
Kustomization. Most source Secrets live in `ducktape-flux`; a source may retain
an existing namespace when that name and namespace are an established consumer
contract.

## Ownership and authorization flow

- The supplier owns the canonical Secret, an exact-name `get`-only Role, and a
  RoleBinding for each approved consumer ServiceAccount. These are reconciled
  from `cluster/k8s/external-creds`; generated grants are collected in
  `external-creds.k8s.yaml`. The explicit authorization roster is
  [`external_creds.py`](external_creds.py). Regenerate
  manifests with `bb run //cluster/cdk8s:generate_manifests`.
- `external-secrets-config` owns the shared `ClusterSecretStore`. Referent
  authentication resolves its `external-creds-reader` ServiceAccount in the
  consuming ExternalSecret's namespace; the store omits the ServiceAccount
  `namespace` field.
- The consumer owns its `external-creds-reader` ServiceAccount and
  ExternalSecret.
- The consumer Flux Kustomization depends on both `external-creds` and
  `external-secrets-config`. Static suppliers do not depend on ESO or consumer
  namespaces: a RoleBinding may name a ServiceAccount before that namespace or
  identity exists.

Tana's rotating Firebase refresh token is the dynamic-source exception. A SOPS
bootstrap Secret and an `OnChange` ExternalSecret in `external-creds` seed one
ESO-owned runtime Secret in `ducktape-flux`. The Tana resigner has a separate
exact-name `get`/`patch` grant for that runtime Secret. It writes only the
central Secret; consumer-owned ExternalSecrets poll it and update independent
copies in `tana-mcp` and `litellm`. This keeps distribution in ESO and out of
the resigner. Because the bridge is part of `external-creds`, that Flux
Kustomization depends on `external-secrets-config`.
To intentionally reseed after token revocation, update the SOPS seed and bump
the bridge's `external-secrets.io/force-sync` annotation in Git.

Approval stays at the source. Referencing the shared ClusterSecretStore does
not grant access: the Kubernetes provider cannot read a canonical Secret
unless `external-creds` contains a RoleBinding for that namespace's
`external-creds-reader` ServiceAccount. The store's namespace conditions mirror
the approved namespaces as defense in depth. The approved namespace remains the
trust boundary: workloads or operators able to use its approved identity can
receive the credential.

## Adding a credential or consumer

Add a static credential by creating one encrypted source Secret under
`cluster/k8s/external-creds` and adding its non-secret metadata to `CREDENTIALS`
in `external_creds.py`. Add a consumer by adding its explicit
`ApprovedConsumer` entry there, then add the ServiceAccount, ExternalSecret,
and namespace to the shared store's conditions. A namespace needs only one
`external-creds-reader` ServiceAccount even when it receives multiple approved
credentials.

## Moving existing resources

When moving live resources between Flux Kustomizations, follow Flux's staged
ownership-transfer procedure: disable pruning on the old owner, reconcile the
move and verify the new inventory, then restore pruning. Do not perform the move
in one reconciliation with pruning enabled.

## Scope

This pattern excludes cluster-internal credentials, Kubernetes-native identity
and PKI material, and credentials minted or rotated by a controller. Tana's
Firebase refresh token uses the documented bootstrap/runtime bridge above;
other OAuth and session credentials (for example CLIProxyAPI) remain explicit
exceptions. Normal workloads use scoped LiteLLM virtual keys instead of vendor
credentials where that is available.
