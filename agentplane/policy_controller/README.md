# Policy Controller

Deletes `PolicyBinding`s, `ServiceAccountGrant`s and `ManagedServiceAccount`s past their
`expiresAt`; keeps the native RoleBindings and ClusterRoleBindings that bindings' Kubernetes rules
and grants' `KubernetesRoleSet`s expand to; and keeps the ServiceAccount each
`ManagedServiceAccount` stands for. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel).

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`../policy/api/v1alpha1/types.go`](../policy/api/v1alpha1/types.go): the neutral CRD types. Bazel generates the
  controller-gen deep-copy methods when building the API; build `//agentplane/policy/api/v1alpha1:deepcopy` to check the generated
  file directly. The pinned upstream `controller-gen` binary and Go SDK run with declared Bazel
  inputs, so no generated Go file is committed.
- [`controller.go`](controller.go): one reconcile per `PolicyBinding`, and the native-binding
  machinery and watches all three controllers share. Sets, subject accounts and the labeled native
  bindings map back to the objects they affect, through field indexes and the native bindings'
  owner annotation; `RequeueAfter` runs an object again at its `expiresAt`, when it is deleted.
- [`grants.go`](grants.go): one reconcile per `ServiceAccountGrant`, the same as a binding's with
  role sets in place of `PolicySet`s.
- [`accounts.go`](accounts.go): one reconcile per `ManagedServiceAccount`, creating its
  ServiceAccount and pruning those whose account is gone.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to what the controller reads.

## Running

Staging runs it from
[`cluster/cdk8s/agentplane/policy_controller.py`](../../cluster/cdk8s/agentplane/policy_controller.py),
which also renders staging's Flux-owned PolicySets from the grant catalog in
[`agent_access_profiles.py`](../../cluster/cdk8s/agent_access_profiles.py) and alerts when a
`PolicyBinding` outlives its `expiresAt`. Its settings are `AGENTPLANE_POLICY_CONTROLLER_*`
environment variables, named by the fields of [`settings.py`](settings.py). Locally:

```bash
AGENTPLANE_POLICY_CONTROLLER_POLICY_NAMESPACE=agentplane-staging \
  AGENTPLANE_POLICY_CONTROLLER_SUBJECT_NAMESPACES=agentplane-staging \
  AGENTPLANE_POLICY_CONTROLLER_ACCOUNTS_NAMESPACE=agentplane-staging-k8s-accounts \
  bb run //agentplane/policy_controller/cmd:policy-controller
```

It uses the in-cluster config or `KUBECONFIG`. Its ServiceAccount needs to list and watch
PolicySets, PolicyBindings, KubernetesRoleSets, ServiceAccountGrants, ManagedServiceAccounts and
ServiceAccounts, delete PolicyBindings, grants and managed accounts, apply their status, read the
referenced Roles and ClusterRoles, list and watch RoleBindings and ClusterRoleBindings
cluster-wide, and create, patch and delete them where sets grant, with `bind` on each referenced
role. In the accounts namespace alone it also creates and deletes ServiceAccounts. It
serves metrics on `:8080` and `/healthz`/`/readyz` on `:8081`, and replicas elect a leader by a
Lease in the policy namespace.

## Design notes

- **No finalizer.** The initial list of labeled native bindings enqueues each one's binding or
  grant, and of labeled ServiceAccounts each one's managed account, so an owner deleted while the
  controller was down is reconciled as not found on start, which deletes what it kept. A finalizer
  would block deletion whenever the controller is down.
- **A holder object for created accounts.** The accounts namespace is separate from the policy
  namespace so the egress proxy's `impersonate` Role there cannot reach the controllers' own
  accounts. OwnerReferences do not cross namespaces, so a ServiceAccount there can carry neither an
  owner nor an expiry; `ManagedServiceAccount` in the policy namespace holds both, and what the
  account owns is garbage-collected with it.
- **Roles are watched by metadata only**, and read uncached. A Role or ClusterRole change
  reconciles the bindings whose `status.missingRoles` names it, so a binding to a role created
  later is granted once it appears. Condition reasons are informational and nothing branches on them.
- The CRD types are independent of the controller so every reader can depend on the API package;
  the Python models of the other rule types remain in their owning services;
  the CRD YAML in `agentplane/crds` is the schema both follow.
- Most tests drive `Reconcile` directly, against controller-runtime's fake client or a real API
  server; only role creation is tested through the controller's watches.

The same binary also serves `PolicyService` gRPC on `:8082` using TokenReview with the
`agentplane-policy` audience. Its read watches run on every replica independently of reconciliation
leader election. Operator grant, revoke and Identity delete calls are admitted only for `writer_accounts`, separately
from the workload read/watch resolver; see the [service contract](SPEC.md#policy-resource-watches).
