# Policy Controller

Deletes `PolicyBinding`s past their `expiresAt`, and keeps the native RoleBindings and
ClusterRoleBindings that their Kubernetes rules expand to. Contract: [`SPEC.md`](SPEC.md). The CRDs
are in [`agentplane/crds`](../crds/BUILD.bazel).

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`../policy/api/v1alpha1/types.go`](../policy/api/v1alpha1/types.go): the neutral CRD types. Bazel generates the
  controller-gen deep-copy methods when building the API; build `//agentplane/policy/api/v1alpha1:deepcopy` to check the generated
  file directly. The pinned upstream `controller-gen` binary and Go SDK run with declared Bazel
  inputs, so no generated Go file is committed.
- [`controller.go`](controller.go): one reconcile per `PolicyBinding`. Sets, subject accounts and
  the labeled native bindings map back to the bindings they affect, through field indexes and the
  native bindings' binding annotation; `RequeueAfter` runs a binding again at its `expiresAt`, when it is deleted.
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
  bb run //agentplane/policy_controller/cmd:policy-controller
```

It uses the in-cluster config or `KUBECONFIG`. Its ServiceAccount needs to list and watch
PolicySets, PolicyBindings and ServiceAccounts, delete PolicyBindings, apply PolicyBinding status,
read the referenced Roles and ClusterRoles, list and watch RoleBindings and ClusterRoleBindings
cluster-wide, and create, patch and delete them where sets grant, with `bind` on each referenced role. It
serves metrics on `:8080` and `/healthz`/`/readyz` on `:8081`, and replicas elect a leader by a
Lease in the policy namespace.

## Design notes

- **No finalizer.** The initial list of labeled native bindings enqueues each one's binding, so a
  `PolicyBinding` deleted while the controller was down is reconciled as not found on start, which
  deletes its native bindings. A finalizer would block deletion whenever the controller is down.
- **Roles are watched by metadata only**, and read uncached. A Role or ClusterRole change
  reconciles the bindings whose `status.missingRoles` names it, so a binding to a role created
  later is granted once it appears. Condition reasons are informational and nothing branches on them.
- The CRD types are independent of the controller so every reader can depend on the API package;
  the Python models of the other rule types remain in their owning services;
  the CRD YAML in `agentplane/crds` is the schema both follow.
- Most tests drive `Reconcile` directly, against controller-runtime's fake client or a real API
  server; only role creation is tested through the controller's watches.
