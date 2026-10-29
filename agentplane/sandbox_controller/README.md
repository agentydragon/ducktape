# Sandbox Controller

Reconciles `Sandbox` into retained state and optional workspace PVCs, its own ServiceAccount and
`PolicyBinding`, and, while running, one Pod or KubeVirt VM from its `SandboxTemplate`. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel). The integration app submits Sandbox desired state and
operator grants through the Kubernetes API; this controller reconciles each Sandbox and owns its
status. Each runner serves mTLS on its workload network, and the Runner Gateway connects to the
current incarnation using its per-incarnation certificate. History and telemetry use the egress
sidecar's platform routes. `spec.bootstrap` and the Sandbox's identity reach its runner
through the Sandbox's launch ConfigMap (a Pod mounts it, a VM gets it as a disk). Session launch
defaults live in the app's preset catalog and are resolved before an opening reaches the Dispatcher.
Deletion first has the runner seal every
Session: the controller calls `Runner.Quiesce` through the [Runner Gateway](../runner_gateway/README.md)'s
[runner proxy](../docs/runner_control.md#runner-proxy), at the Sandbox's own control Service, and sets the
`Quiesced` condition. Suspension seals the same way before the Pod stops, recorded as
`status.quiescedIncarnation`. `runner_gateway_target` and `runner_gateway_token_file` are required settings.
Storage layouts, compatible attachments and retained identities: [Sandbox storage](../docs/sandbox_storage.md).

`runner_certificate_issuer` names the cert-manager Issuer used for per-incarnation `Certificate`
requests. cert-manager owns key generation and renewal. The controller waits for the matching
TLS Secret before starting a runner and binds its credential ID to the current Pod or VMI UID.
Replacement workloads get distinct Secret names. trust-manager supplies the trusted roots.
See [authentication and cutover](../docs/runner_control.md#authentication-and-incarnation-binding).

Because that condition is set only once the History Service's ingress has
acknowledged every seal, deleting a Sandbox waits while the History Service is down or rolling;
everything else the controller does is unaffected.

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`sandboxes/api`](../sandboxes/api/types.go) contains the shared CRD types and scheme. Bazel
  generates the controller-gen deep-copy methods; build `//agentplane/sandboxes/api:deepcopy` to
  check them directly. The pinned upstream `controller-gen` binary and Go SDK run with declared
  Bazel inputs, so no generated Go file is committed.
- [`controller.go`](controller.go): one reconcile per `Sandbox`. Its VM (`Owns`), its Pods, VMI and
  state PVC (by the Sandbox label) and the templates it names (by a field index) map back to it.
- [`vm.go`](vm.go): a VM Sandbox's VirtualMachine (types from `kubevirt.io/api`), its run strategy,
  and the VMI and virt-launcher Pod its status comes from.
- [`runner_credential.go`](runner_credential.go): Certificate requests, incarnation binding,
  credential mounts and cleanup of retired generations.
- [`state_volume.go`](state_volume.go): the state layout contract, PVC creation record and identity binding.
- [`workspace.go`](workspace.go): workspace layout validation, its PVC creation record and persisted claim binding.
- [`identity.go`](identity.go): the Sandbox's ServiceAccount and `PolicyBinding`, and the
  conflicts that keep a Pod from starting. The `PolicyBinding` types are the Policy Controller's.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to one namespace and, of the kinds
  it creates, to objects carrying the Sandbox label.

Ordinary child resources use narrow server-side apply updates after an uncached ownership
check. Creation is exclusive, so a same-name collision cannot adopt a foreign resource. PVCs
retain their creation records and are never reapplied from a changed template.

## Running

Every Agentplane environment runs it from `cluster/cdk8s/agentplane/sandbox_controller.py`, which
also holds its RBAC; its runner `SandboxTemplate`s are in `runner_sandbox.py`.
Its settings are `AGENTPLANE_SANDBOX_CONTROLLER_*` environment variables, named by the fields of
[`settings.py`](settings.py). Locally:

```bash
AGENTPLANE_SANDBOX_CONTROLLER_NAMESPACE=<namespace> \
  AGENTPLANE_SANDBOX_CONTROLLER_RUNNER_GATEWAY_TARGET=<target> \
  AGENTPLANE_SANDBOX_CONTROLLER_RUNNER_GATEWAY_TOKEN_FILE=<token file> \
  AGENTPLANE_SANDBOX_CONTROLLER_RUNNER_CERTIFICATE_ISSUER=<issuer> \
  bb run //agentplane/sandbox_controller/cmd:sandbox-controller
```

At startup the controller discovers whether the cluster serves KubeVirt's API and only then watches
VirtualMachines and VMIs. Without that API, VM Sandboxes report `Running=False` with reason
`KubeVirtMissing`, and Pod Sandboxes are unaffected. It uses the
in-cluster config or `KUBECONFIG`, serves metrics on `:8080` and `/healthz`/`/readyz`
on `:8081`, and replicas elect a leader by a Lease in the namespace.

## Design notes

- **A finalizer on every Sandbox**, unlike the Policy Controller: the seal and the Pod or VM
  must be done before the Sandbox, and with it the owned state PVC, can go.
- **The PVC's owner reference does not block deletion.** Under foreground propagation or namespace
  deletion a blocking reference would remove the state while a sealing runner still writes to it.
- **The Pod is deleted before the finalizer clears**, in separate reconciles, so the PVC, which
  garbage collection removes afterwards, never goes while a container can still write to it.
- **The runner seals, the controller asks.** The seal is a unary `Runner.Quiesce` through the gateway
  proxy. The controller records the last request time for that runner incarnation before calling,
  then waits up to one minute before retrying a failed or unfinished request. Runner stage changes
  enqueue it sooner; `quiesced` completes without another call. The recorded deadline survives
  watch events and controller restarts. The controller never holds a runner connection. A Sandbox
  with no connection Lease, the Runner Gateway's record that a runner ever connected, has nothing to
  seal and is deleted without `Quiesced`.
- **The idle timer is controller memory, started at its first sight of each `idle-since` value.**
  The runner's clock never counts, so skew cannot suspend early; a restart starts every timer
  over and suspends up to one `idleSuspendAfter` later. `status.idleSuspendAt` reports the next
  attempt as an absolute time; it stays stable during the countdown, advances after a retry, and
  clears when no attempt is scheduled. The controller sends the runner's `idle-since` value as
  `Quiesce(if_idle_since)`: work that arrived after the decision makes the runner refuse with
  `ABORTED` and the timer starts over. Another failure retries at the quiesce deadline.
- **A wake during the idle `Quiesce` is not lost.** The accepted request is recorded by the status
  write that follows, at the wake generation read before the call, so a wake in between fails that
  write on the stale resource version. The next reconcile finds a runner in lameduck with no
  recorded cause; the Sandbox is to run, so the stop path seals it and replaces the incarnation.
  A restart between the call and the write ends the same way, at the cost of one incarnation.
- **`RunnerConnected` watches the Lease.** A new connection, a different runner Pod or a change in
  the runner's published facts (quiesce stage, idle-since, bootstrap state, setup state, autosuspend
  blockers, valid only while the Lease's `pod-uid` is the incarnation's) enqueues the Sandbox;
  renewals do not. Time is not an event, so the moment a live connection would lapse, and a lapsed
  one, are requeued.
- **Gotcha:** a deleted Sandbox whose runner cannot seal stays in `Quiescing`. To give up on the seal,
  remove the finalizer by hand: the PVC is owned, so it goes with the Sandbox, and the Pod or VM
  with it.
- **No Pod waits on its `PolicyBinding`'s readers.** Every reader fails closed, so a grant applied
  after the Pod starts only refuses its first requests, as for a running Pod whose PolicySets
  change; a gate would make every Sandbox depend on every reader, optional ones included.
- **Gotcha:** agent-sandbox's `Sandbox`, which the cluster installs for other workloads, makes
  `kubectl get sandboxes` resolve to either kind; name ours as `sandboxes.agentplane.allegedly.works`.
- Unit tests drive `Reconcile` against controller-runtime's fake client. The child-resource
  integration test runs the manager against an `envtest` API server to cover filtered-cache
  collisions, SSA ownership and drift correction. KubeVirt behavior is not simulated: unit
  tests create VMIs and launcher Pods by hand.
- **Gotcha:** the fake client's default managed-fields tracker panics on KubeVirt's `uint64`
  fields, so the tests build it on a plain object tracker.
