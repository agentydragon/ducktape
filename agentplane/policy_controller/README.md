# Policy Controller

Deletes `PolicyBinding`s past their `expiresAt`, and keeps the native RoleBindings and
ClusterRoleBindings that their Kubernetes rules expand to. Contract: [`SPEC.md`](SPEC.md). The CRDs
are in [`agentplane/crds`](../crds/BUILD.bazel).

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`types.go`](types.go): the CRD types. Bazel generates the controller-gen deep-copy methods when
  building the controller; build `//agentplane/policy_controller:deepcopy` to check the generated
  file directly. The pinned upstream `controller-gen` binary and Go SDK run with declared Bazel
  inputs, so no generated Go file is committed.
- [`controller.go`](controller.go): one reconcile per `PolicyBinding`. Sets, subject accounts and
  the labeled native bindings map back to the bindings they affect, through field indexes and the
  native bindings' binding annotation; `RequeueAfter` runs a binding again at its `expiresAt`, when it is deleted.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to what the controller reads.

## Running

Staging and `agentplane-testing` run it from
[`cluster/cdk8s/agentplane/policy_controller.py`](../../cluster/cdk8s/agentplane/policy_controller.py),
which also renders staging's Flux-owned PolicySets from the grant catalog in
[`agent_access_profiles.py`](../../cluster/cdk8s/agent_access_profiles.py) and alerts when a
`PolicyBinding` outlives its `expiresAt`. Locally:

```bash
bb run //agentplane/policy_controller/cmd:policy-controller -- \
  --policy-namespace=agentplane-staging --subject-namespaces=agentplane-staging
```

It uses the in-cluster config or `KUBECONFIG`. Its ServiceAccount needs to list and watch
PolicySets, PolicyBindings and ServiceAccounts, delete PolicyBindings, apply PolicyBinding status,
read the referenced Roles and ClusterRoles, list and watch RoleBindings and ClusterRoleBindings
cluster-wide, and create and delete them where sets grant, with `bind` on each referenced role. It
serves metrics on `:8080` and `/healthz`/`/readyz` on `:8081`, and replicas elect a leader by a
Lease in the policy namespace.

## Design notes

- **No finalizer.** The initial list of labeled native bindings enqueues each one's binding, so a
  `PolicyBinding` deleted while the controller was down is reconciled as not found on start, which
  deletes its native bindings. A finalizer would block deletion whenever the controller is down.
- **Roles are watched by metadata only**, and read uncached. Any Role or ClusterRole change
  reconciles the bindings whose condition says a role is missing, so a binding to a role created
  later is granted once it appears.
- The CRD types exist again as the Python models of the services that will read the other rule types;
  the CRD YAML in `agentplane/crds` is the schema both follow.
- Tests drive `Reconcile` against controller-runtime's fake client, so which events enqueue which
  binding is not covered.
