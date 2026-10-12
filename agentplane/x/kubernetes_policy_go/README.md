# Kubernetes Policy Controller, Go spike

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) version of
[`agentplane/kubernetes_policy`](../../kubernetes_policy/SPEC.md), for comparing the two. It reads
the same `PolicySet` and `PolicyBinding` CRDs and keeps the same native bindings, labels and names.

- [`types.go`](types.go): the CRD types; `zz_generated.deepcopy.go` is controller-gen output
  (`controller-gen object paths=./agentplane/x/kubernetes_policy_go`).
- [`controller.go`](controller.go): one reconcile per `PolicyBinding`. Sets, accounts and the
  labeled native bindings map back to the bindings they affect through field indexes and an
  annotation; `RequeueAfter` runs a binding again at its `expiresAt`.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to what the controller reads.

Not done: the `PolicySet` condition, a separate `PolicySetUnavailable` reason, deployment. The tests
drive `Reconcile` against controller-runtime's fake client, so the watch wiring is untested; envtest
would cover it but needs `kube-apiserver` and `etcd` binaries.

```bash
bbr test //agentplane/x/kubernetes_policy_go/...
```
