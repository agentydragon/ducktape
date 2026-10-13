# Sandbox Controller

Reconciles `Sandbox` into a state PVC that outlives every incarnation, its own ServiceAccount and
`PolicyBinding`, and, while running, one Pod or KubeVirt VM from its `SandboxTemplate`. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel). The [Sandbox Service](../sandbox_service/README.md)'s
`CreateSandbox` writes these Sandboxes. Its runner template reaches platform services over the
egress sidecar's platform routes. `spec.sessionDefaults` is the Runner
Gateway's to read; `spec.bootstrap` and the Sandbox's identity reach its runner through the Sandbox's
launch ConfigMap (a Pod mounts it, a VM gets it as a disk). Deletion first has the runner seal every
Session: the controller calls `Runner.Seal` through the [Runner Gateway](../runner_gateway/README.md)'s
[runner proxy](../docs/runner_control.md#runner-proxy), at the Sandbox's own control Service, and sets the
`SessionsSealed` condition.

Because the controller sets that condition only once the History Service's ingress has
acknowledged every seal, deleting a Sandbox waits while the History Service is down or rolling;
everything else the controller does is unaffected.

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`types.go`](types.go): the CRD types. `zz_generated.deepcopy.go` is controller-gen output; after
  changing the types, regenerate it with
  `go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0 object paths=./agentplane/sandbox_controller`
  from a module that resolves this package.
- [`controller.go`](controller.go): one reconcile per `Sandbox`. Its VM (`Owns`), its Pods, VMI and
  state PVC (by the Sandbox label) and the templates it names (by a field index) map back to it.
- [`vm.go`](vm.go): a VM Sandbox's VirtualMachine (types from `kubevirt.io/api`), its run strategy,
  and the VMI and virt-launcher Pod its status comes from.
- [`identity.go`](identity.go): the Sandbox's ServiceAccount and `PolicyBinding`, and the
  conflicts that keep a Pod from starting. The `PolicyBinding` types are the Policy Controller's.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to one namespace and, of the kinds
  it creates, to objects carrying the Sandbox label.

## Running

Every Agentplane environment runs it from `cluster/cdk8s/agentplane/sandbox_controller.py`, which
also holds its RBAC; its runner `SandboxTemplate`s are in `runner_sandbox.py`.
Locally:

```bash
bb run //agentplane/sandbox_controller/cmd:sandbox-controller -- --namespace=<namespace>
```

Where the cluster serves KubeVirt's API it watches VirtualMachines and VMIs; where not, VM Sandboxes
stay unready (`KubeVirtMissing`) and Pod Sandboxes are unaffected. It uses the
in-cluster config or `KUBECONFIG`, serves metrics on `:8080` and `/healthz`/`/readyz`
on `:8081`, and replicas elect a leader by a Lease in the namespace.

## Design notes

- **A finalizer on every Sandbox**, unlike the Policy Controller: the state PVC has no owner
  reference by design, so only the controller can delete it, and a Sandbox deleted while the
  controller is down has to wait for it.
- **The Pod is deleted before the PVC**, in separate reconciles, so the PVC never goes while a
  container can still write to it.
- **The runner seals, the controller asks.** The seal is a unary `Runner.Seal` through the gateway
  proxy, repeated every few seconds until every seal is flushed; the controller never holds a runner
  connection. A Sandbox with no connection Lease, the Runner Gateway's record that a runner ever
  connected, has nothing to seal.
- **Gotcha:** a deleted Sandbox whose runner cannot seal stays in `Sealing`. Removing the finalizer
  by hand orphans the PVC, which has no owner reference; to give up on the seal, set
  `SessionsSealed` True on the Sandbox's status instead, and the controller deletes the rest.
- **No Pod waits on its `PolicyBinding`'s readers.** Every reader fails closed, so a grant applied
  after the Pod starts only refuses its first requests, as for a running Pod whose PolicySets
  change; a gate would make every Sandbox depend on every reader, optional ones included.
- **Gotcha:** agent-sandbox's `Sandbox`, which the cluster installs for other workloads, makes
  `kubectl get sandboxes` resolve to either kind; name ours as `sandboxes.agentplane.allegedly.works`.
- Tests drive `Reconcile` against controller-runtime's fake client, so which events enqueue which
  Sandbox is not covered, nor anything KubeVirt does: the tests create VMIs and launcher Pods by
  hand.
- **Gotcha:** the fake client's default managed-fields tracker panics on KubeVirt's `uint64`
  fields, so the tests build it on a plain object tracker.
