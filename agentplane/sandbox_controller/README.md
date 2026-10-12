# Sandbox Controller

Reconciles `Sandbox` into a state PVC that outlives every incarnation and, while running, one Pod
from its `SandboxTemplate`. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel). It is the Go controller the
[Sandbox Service](../sandbox_service/README.md) is moving into, and does not yet do what that
service does around a Sandbox: its ServiceAccount, grants, egress and Action policy bindings,
runner endpoints, launch binding and the teardown seal. Until it does, nothing creates these
Sandboxes. Deleting one deletes its PVC without sealing its Sessions.

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`types.go`](types.go): the CRD types. `zz_generated.deepcopy.go` is controller-gen output; after
  changing the types, regenerate it with
  `go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0 object paths=./agentplane/sandbox_controller`
  from a module that resolves this package.
- [`controller.go`](controller.go): one reconcile per `Sandbox`. Its Pod (`Owns`), its state PVC
  (by the Sandbox label) and the templates it names (by a field index) map back to it.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to one namespace and, of Pods and
  PVCs, to those carrying the Sandbox label.

## Running

Not deployed yet. Locally:

```bash
bb run //agentplane/sandbox_controller/cmd:sandbox-controller -- --namespace=<namespace>
```

It uses the in-cluster config or `KUBECONFIG`. Its ServiceAccount needs to list and watch
Sandboxes and SandboxTemplates, update Sandboxes and their status, and create, delete, list and
watch Pods and PVCs in its namespace. It serves metrics on `:8080` and `/healthz`/`/readyz` on
`:8081`, and replicas elect a leader by a Lease in the namespace.

## Design notes

- **A finalizer on every Sandbox**, unlike the Policy Controller: the state PVC has no owner
  reference by design, so only the controller can delete it, and a Sandbox deleted while the
  controller is down has to wait for it.
- **The Pod is deleted before the PVC**, in separate reconciles, so the PVC never goes while a
  container can still write to it.
- **Gotcha:** while agent-sandbox's `Sandbox` is installed, `kubectl get sandboxes` may resolve to
  either kind; name ours as `sandboxes.agentplane.allegedly.works`.
- Tests drive `Reconcile` against controller-runtime's fake client, so which events enqueue which
  Sandbox is not covered.
