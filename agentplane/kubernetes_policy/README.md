# Kubernetes Policy Controller

Keeps the native RoleBindings and ClusterRoleBindings that `PolicyBinding`s' Kubernetes rules expand
to. Contract: [`SPEC.md`](SPEC.md). The CRDs are in [`agentplane/crds`](../crds/BUILD.bazel).

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`types.go`](types.go): the CRD types. `zz_generated.deepcopy.go` is controller-gen output; after
  changing the types, regenerate it with
  `go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0 object paths=./agentplane/kubernetes_policy`
  from a module that resolves this package.
- [`controller.go`](controller.go): one reconcile per `PolicyBinding`. Sets, subject accounts and
  the labeled native bindings map back to the bindings they affect, through field indexes and the
  native bindings' binding annotation; `RequeueAfter` runs a binding again at its `expiresAt`.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to what the controller reads.

## Running

```bash
bb run //agentplane/kubernetes_policy/cmd:kubernetes-policy-controller -- \
  --policy-namespace=agentplane-staging --subject-namespaces=agentplane-staging
```

It uses the in-cluster config or `KUBECONFIG`. Its ServiceAccount needs to list and watch
PolicySets, PolicyBindings and ServiceAccounts, apply PolicyBinding status, read the referenced Roles
and ClusterRoles, list and watch RoleBindings and ClusterRoleBindings cluster-wide, and create and
delete them where sets grant, with `bind` on each referenced role.

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
