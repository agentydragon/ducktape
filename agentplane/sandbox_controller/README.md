# Sandbox Controller

Reconciles `Sandbox` into a state PVC that outlives every incarnation, its own ServiceAccount and
`PolicyBinding`, and, while running, one Pod from its `SandboxTemplate`. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel). It is the Go controller the
[Sandbox Service](../sandbox_service/README.md) is moving into, and does not yet do what that
service does around a Sandbox: runner endpoints and launch binding. `spec.sessionDefaults` and
`spec.bootstrap` are the Runner Gateway's to read; the controller only carries them. Until
`CreateSandbox` writes our kind, nothing creates these Sandboxes; the Sandbox Service, Runner
Gateway and app already read them. Deletion waits for the
[Runner Gateway](../runner_gateway/README.md)'s `SessionsSealed` condition.

Because the Runner Gateway sets that condition only once the History Service's ingress has
acknowledged every seal, deleting a Sandbox waits while the History Service is down or rolling;
everything else the controller does is unaffected.

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`types.go`](types.go): the CRD types. `zz_generated.deepcopy.go` is controller-gen output; after
  changing the types, regenerate it with
  `go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0 object paths=./agentplane/sandbox_controller`
  from a module that resolves this package.
- [`controller.go`](controller.go): one reconcile per `Sandbox`. Its Pod (`Owns`), its state PVC
  (by the Sandbox label) and the templates it names (by a field index) map back to it.
- [`identity.go`](identity.go): the Sandbox's ServiceAccount and `PolicyBinding`, and the
  conflicts that keep a Pod from starting. The `PolicyBinding` types are the Policy Controller's.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to one namespace and, of the kinds
  it creates, to objects carrying the Sandbox label.

## Running

Environments with `own_sandboxes` (`agentplane-testing` only, for now) run it from
`cluster/cdk8s/agentplane/sandbox_controller.py`, which also holds its RBAC, and get a runner
`SandboxTemplate` of our kind beside each of agent-sandbox's (`runner_sandbox.py`).
Locally:

```bash
bb run //agentplane/sandbox_controller/cmd:sandbox-controller -- --namespace=<namespace>
```

It uses the in-cluster config or `KUBECONFIG`, serves metrics on `:8080` and `/healthz`/`/readyz`
on `:8081`, and replicas elect a leader by a Lease in the namespace.

## Design notes

- **A finalizer on every Sandbox**, unlike the Policy Controller: the state PVC has no owner
  reference by design, so only the controller can delete it, and a Sandbox deleted while the
  controller is down has to wait for it.
- **The Pod is deleted before the PVC**, in separate reconciles, so the PVC never goes while a
  container can still write to it.
- **The gateway seals, the controller deletes.** The Runner Gateway holds the runner's control
  stream, so it seals and reports through the `SessionsSealed` condition; the controller makes no
  gRPC call.
- **Gotcha:** a deleted Sandbox whose runner cannot seal stays in `Sealing`. Removing the finalizer
  by hand orphans the PVC, which has no owner reference; to give up on the seal, set
  `SessionsSealed` True on the Sandbox's status instead, and the controller deletes the rest.
- **No Pod waits on its `PolicyBinding`'s readers.** Every reader fails closed, so a grant applied
  after the Pod starts only refuses its first requests, as for a running Pod whose PolicySets
  change; a gate would make every Sandbox depend on every reader, optional ones included.
- **Gotcha:** while agent-sandbox's `Sandbox` is installed, `kubectl get sandboxes` may resolve to
  either kind; name ours as `sandboxes.agentplane.allegedly.works`.
- Tests drive `Reconcile` against controller-runtime's fake client, so which events enqueue which
  Sandbox is not covered.
