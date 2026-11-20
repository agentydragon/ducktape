# Sandbox Controller

Environments serves EnvironmentService on every replica at `environments:8082`, using the
existing Sandbox Controller service SVID. Reconcilers and lifecycle-fact observation stay leader-only;
the listener and its independent policy watches determine readiness on leaders and standbys. See
[the serving contract](SPEC.md#environmentservice-listener). App and Actions use this receiver with
service SVIDs. Actions preserves effective-Identity delegation for list/get/resume and the separate
launcher grant for reviewed orchestration. Managed Sandbox CLIs use the dedicated local EnvironmentService route. The Sessions
binary does not serve EnvironmentService; automatic wake stays with Sessions.
Explicit resume is implemented in `internal/resume` without a wake-budget dependency. Dispatcher
owns automatic wake execution and budgeting; the two owners share `environments/lifecycle` policy
and guarded writes, with independent policy views and no Environments-to-Sessions call.
Enabling `environment_service` requires the accepting Controller image to be pinned first; remove
that configuration before rolling back to an image that cannot decode it.

Deployment enables `environment_service.runtime_proxy` to serve the existing Runtime proxy and
Executor on every replica at `environments:8082`, with independent routing watches and narrow
service SVID grants. Its outbound `runtime-proxy` certificate is separate from the Controller's
binding observer certificate. Sessions delivery, central sampler Executor and Controller lifecycle
calls use the proxy and pin its Sandbox Controller server identity. App diagnostics
use `EnvironmentService.GetRuntimeDiagnostics` on the same listener; the App has no raw-proxy grant. The Sessions
binary has no Runtime proxy or Executor receiver and starts no direct Runtime watches.
Proxy admission uses service SVIDs directly; `runtime_proxy` has no bearer audience setting.
See [the receiver contract](SPEC.md#optional-runtime-proxy-and-executor-receiver).

EnvironmentService admits workload and exact-role service SVIDs only, without TokenReview. Configure
service SVID allowlists and typed service delegation. The staging render grants no TokenReview
(`test_workload_trust.py`) and Sandbox Pods project no Dispatcher or History audience token
(`test_egress.py`), so no projected-token cleanup remains here.

Reconciles `Sandbox` into retained state and optional workspace PVCs and, while running as the
`Identity` its spec names, one Pod or KubeVirt VM from its `SandboxTemplate`. Contract: [`SPEC.md`](SPEC.md). The CRDs are in
[`agentplane/crds`](../crds/BUILD.bazel). The integration app submits Sandbox desired state and
operator grants through the Kubernetes API; this controller reconciles each Sandbox and owns its
status. Each runner serves mTLS on its workload network, and the Environments proxy connects to the
current incarnation using its per-incarnation certificate. The elected Controller observes Runtime
facts directly with a narrow SVID; that authenticated watch binds the current workload UID before
publishing facts, while proxy watches cannot select or replace the binding. History and telemetry
use the egress sidecar's platform routes. `spec.bootstrap` and the Sandbox's identity reach its runner
through the Sandbox's launch ConfigMap (a Pod mounts it, a VM gets it as a disk). Session launch
defaults live in the app's preset catalog and are resolved before an opening reaches the Dispatcher.
Deletion first has the runner seal every
Session: the controller calls `EnvironmentRuntime.Quiesce` through the
[Runtime proxy](runtimeproxy/SPEC.md) and sets the
`Quiesced` condition. Suspension seals the same way before the Pod stops, recorded as
`status.quiescedIncarnation`. `runner_security` selects the Controller's own direct observer credentials;
`runner_gateway_target` and `runner_gateway_tls` configure its outbound lifecycle call to
Environments in staging.
Storage layouts, compatible attachments and retained identities: [Sandbox storage](../docs/sandbox_storage.md).

Each incarnation runs with the Sandbox's `WorkloadCredential`, which the controller requests
through [`identity/credentials/requester`](../identity/credentials/README.md) and the Policy Controller
issues; the controller creates no Certificate. It starts a runner only once the record is issued,
binds the record to the Pod or VMI it started, and publishes that incarnation once the record
accepted it. See [authentication and cutover](../docs/runner_control.md#authentication-and-incarnation-binding).
The issued Secret permits both server auth for Runner control and client auth for the egress
sidecar's separately selected mTLS proxy hop. Pod projections go to Runtime and, when present,
egress-sidecar; runtime-only custom templates remain supported. Workload containers do not mount
it. For a KubeVirt launcher relay, admission checks the record's accepted binding against the
current VMI before mounting that Secret into the sidecar; it does not mount it into a workload
container.

Because that condition is set only once the History Service's ingress has
acknowledged every seal, deleting a Sandbox waits while the History Service is down or rolling;
everything else the controller does is unaffected.

A [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime) controller:

- [`sandboxes/api`](../sandboxes/api/types.go) contains the shared CRD types and scheme. Bazel
  generates the controller-gen deep-copy methods; build `//agentplane/sandboxes/api:deepcopy` to
  check them directly. The pinned upstream `controller-gen` binary and Go SDK run with declared
  Bazel inputs, so no generated Go file is committed.
- [`controller.go`](internal/controller/controller.go): one reconcile per `Sandbox`. Its VM (`Owns`), its ConfigMap,
  WorkloadCredential, its Pods, VMI and state PVC (by the Sandbox label), and the
  templates and Identity it names (by a field index) map back to it.
- [`vm.go`](internal/controller/vm.go): a VM Sandbox's VirtualMachine (types from `kubevirt.io/api`), its run strategy,
  and the VMI and virt-launcher Pod its status comes from.
- [`internal/runtimecredentials`](internal/runtimecredentials): credential decisions, Identity subject fences
  and WorkloadCredential requests. It receives object snapshots and holds no Kubernetes client.
  The controller reads those objects and applies its closed outcomes, with VM writes in `vm.go`,
  Pod replacement through the existing delete path and credential binding in the final status write
  (the Firecracker launcher UID is persisted earlier for launcher-loss recovery).
- [`environment_runtime_credential.go`](internal/controller/environment_runtime_credential.go): credential mounts.
- [`state_volume.go`](internal/controller/state_volume.go): the state layout contract, PVC creation record and identity binding.
- [`workspace.go`](internal/controller/workspace.go): workspace layout validation, its PVC creation record and persisted claim binding.
- [`identity.go`](internal/controller/identity.go): the live `Identity` gate and object reads for credential subject decisions.
  A retained legacy credential without an Identity is revoked before
  the next start; a running incarnation with one is replaced.
- [`cmd/main.go`](cmd/main.go): the manager, its cache limited to one namespace.
- [`cache.go`](internal/controller/cache.go): the cache holds only objects carrying the Sandbox label (ConfigMaps,
  and the other kinds it creates or watches), every Identity and WorkloadCredential in the
  namespace and Pod Events. Runtime facts come only from direct in-memory observation. The
  Controller does not read per-Sandbox connection Leases or create per-Sandbox routing Services or
  EndpointSlices; its Lease permission is for leader election.
  RBAC cannot select by label, so the Role
  still grants `list`/`watch` on those kinds.

Ordinary child resources use narrow server-side apply updates after an uncached ownership
check. Creation is exclusive, so a same-name collision cannot adopt a foreign resource. PVCs
retain their creation records and are never reapplied from a changed template.

## Running

The cdk8s construct in `cluster/cdk8s/agentplane/sandbox_controller.py` owns the Environments
Deployment and RBAC. SandboxTemplates live in `environment_sandbox.py`. CI publishes the serving
image as `agentplane-environments`. The process reads strict YAML settings from the path in
`AGENTPLANE_ENVIRONMENTS_CONFIG_FILE`. Static settings are authored in [`settings.py`](settings.py).

```bash
AGENTPLANE_ENVIRONMENTS_CONFIG_FILE=<settings.yaml> \
  bb run //agentplane/environments/cmd:sandbox-controller
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
- **The runner seals, the controller asks.** The seal is a unary `EnvironmentRuntime.Quiesce` through the Environments
  proxy. The controller records the last request time for that runner incarnation before calling,
  and makes at most three attempts on transient `Unavailable` replies within one 30-second handshake, opening a new connection through the stable
  Environments Service on each try and fencing every call to that same incarnation Pod UID. A changed
  incarnation or refused caller ends the handshake. A failed or unfinished handshake then waits
  up to one minute before another attempt. Runner stage changes
  enqueue it sooner; `quiesced` completes without another call. The recorded deadline survives
  watch events and controller restarts. A published incarnation is conservatively sealed even after leadership takeover has lost its
  observation history; empty memory cannot establish that it admitted no work. Its unsealed
  published UID stays in status if the Pod disappears, so deletion still recovers retained journals.
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
- **The elected leader watches Runtime directly.** `WatchState(observe_only=true)` uses the
  Controller's own SVID and grants no Runtime command authority. This
  authenticated request binds the current workload UID for that Runtime process before facts are
  published; reconnects may retry only that UID. `runtime_states.go` keeps snapshots in memory by
  Sandbox UID and Pod UID, with boot ID and a local stream generation. A replacement fences late
  callbacks. New leaders discover current incarnations through the initial cache list and reopen
  watches; they wait for an authenticated snapshot before reporting connectivity or idle
  eligibility. Lost streams retry after one second. A 30-second heartbeat timeout makes idle facts
  ineligible immediately; connectivity keeps a one-minute grace. Reopening a stream restarts its
  full idle timer, including when `idle-since` is unchanged. Quiesce retry deadlines remain durable;
  only fresh observed quiescence or an acknowledged quiesce RPC completes a seal. Bootstrap failure
  stays terminal for that incarnation. Every Environments proxy replica independently observes the
  Controller-bound Runtime UID and may call using the current Sandbox UID,
  workload UID and Runtime boot ID.
- **Gotcha:** a deleted Sandbox whose runner cannot seal stays in `Quiescing`. To give up on the seal,
  remove the finalizer by hand: the PVC is owned, so it goes with the Sandbox, and the Pod or VM
  with it.
- **No Pod waits on its Identity's bindings' readers.** Every reader fails closed, so a grant applied
  after the Pod starts only refuses its first requests, as for a running Pod whose
  Identity's grants change; a gate would make every Sandbox depend on every reader, optional ones included.
- **Gotcha:** agent-sandbox's `Sandbox`, which the cluster installs for other workloads, makes
  `kubectl get sandboxes` resolve to either kind; name ours as `sandboxes.agentplane.allegedly.works`.
- Unit tests drive `Reconcile` against controller-runtime's fake client. The child-resource
  integration test runs the manager against an `envtest` API server to cover filtered-cache
  collisions, SSA ownership and drift correction. KubeVirt behavior is not simulated: unit
  tests create VMIs and launcher Pods by hand.
- **Gotcha:** the fake client's default managed-fields tracker panics on KubeVirt's `uint64`
  fields, so the tests build it on a plain object tracker.

EnvironmentService handlers, management and views live in [internal/service](internal/service/README.md).
The shared lifecycle manager remains outside that internal boundary until the explicit/automatic
split; automatic wake execution and budget ownership stay with Dispatcher.

Runtime process, Session and lifecycle evidence is available through the App-only
`EnvironmentService.GetRuntimeDiagnostics` RPC and the App's authenticated generated facade at
`GET /upstream/environments/v1/environments/{namespace}/{name}:runtime-diagnostics?target.uid={uid}`.
The App reads this RPC over its existing EnvironmentService connection and App service SVID; the
Controller adds no HTTP listener. The route ships with the App's generated gateway; the backend RPC
already exists. See the [diagnostic contract](SPEC.md#live-runtime-diagnostics).
