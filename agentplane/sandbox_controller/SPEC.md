# Sandbox Controller

Reconciles Agentplane's own `Sandbox` (`agentplane/crds/manifests/crd-sandboxes.yaml`) into Pods,
a KubeVirt VirtualMachine, or a Firecracker launcher Pod, and the state PVC. A Sandbox is its state,
running or not; an **incarnation** is one Pod, or one run of the VM. Its `SandboxTemplate` sets
exactly one of `pod`, `vm`, or `firecracker`.

## State PVC

- Every Sandbox gets one PVC, `<sandbox>-state`, created from its `SandboxTemplate`'s claim when
  the Sandbox is created, whether or not it is running.
- `stateVolume.layout` is required and accepts `DirectoryTree` for Pod and VM templates, or
  `RawDiskImage` with `ext4` for Firecracker; both use a `Filesystem` claim.
  It records the layout on the PVC and pins the claim's name, UID and layout in
  `status.volumes.state` before attaching it. An existing PVC without a creation record is rejected, including a controller-owned claim.
  A missing or changed claim, foreign PVC, lost or unsupported record, changed layout, or changed UID blocks workload
  start without replacing the retained PVC.
- The Sandbox is the PVC's controller owner, without `blockOwnerDeletion`, so the Pod going away or
  stopping never removes it, and foreground propagation or namespace deletion cannot remove it
  before the seal completes. A Sandbox never uses a PVC another Sandbox controls, and reports
  `StateVolumeConflict` instead.
- Deleting the Sandbox removes its PVC by garbage collection once its Sessions are sealed, its Pod or
  VM is gone and the finalizer clears (below). Stopping never does.

## Incarnations

- `status.operatingMode` (`Running` or `Suspended`) is whether the controller runs the Sandbox, and what
  the rest of this section calls running: `spec.running`, unless the controller suspended the Sandbox
  for idleness (`status.suspendedBy: Idle`) and `spec.wakeGeneration` still equals
  `status.suspendedAtWakeGeneration`. A pending `status.firecrackerSuspension` also keeps the mode
  Suspended through admission preparation, checkpoint and thaw, even if a wake arrives. Readers of
  `spec.running` read the mode instead.
- `status.suspendedBy` says why a Sandbox is suspended and is absent while it runs: `Requested` when
  `spec.running` is clear, `Idle` when the controller suspended it for idleness. A requester that
  clears `spec.running` of an idle-suspended Sandbox replaces `Idle` with `Requested`.
  `status.suspendedAtWakeGeneration` is set exactly with `Idle`.
- While `spec.running` is set, a Pod or Firecracker Sandbox has one Pod, named after it and controlled
  by it, with the template's state volume backed by the PVC. A plain Pod that goes away or ends while
  running is replaced by a new incarnation on the same PVC. A Firecracker launcher is held for
  recovery if it disappears or ends, because its retained state may contain a RAM checkpoint.
- Clearing `spec.running` deletes a plain Pod, once the runner that connected from it has sealed every
  Session (`status.quiescedIncarnation` names the sealed Pod). A Pod whose runner never connected has
  nothing to seal. The Sandbox stays held, with `status.stopStage: Quiescing`, while the quiesce fails or
  the ingress has not acknowledged it. The controller persists the last request time and runner Pod
  UID in `status.quiesceAttempt` together with `status.stopStage: Quiescing` before calling the runner,
  so watch events and controller restarts cannot bypass its one-minute
  retry deadline; Lease facts can complete the seal sooner. Applies only to a controller configured
  to seal.
- A resumed Sandbox whose Pod is already sealed gets a new incarnation, as a sealed runner takes no
  new Sessions.
- Optional `spec.idleSuspendAfter`, a duration of at least one minute, suspends an idle Sandbox. Unset,
  the controller never suspends it for idleness. Once the runner has been idle that long, timed on
  the controller's clock from when it first saw that idle period, the controller asks the runner to
  quiesce only if it is still idle since then for Pod and VM templates. A Firecracker Sandbox first
  persists a `Preparing` attempt with a monotonically increasing generation and the observed idle
  timestamp. This fences Gateway dispatch before `PrepareSuspend` atomically checks Runtime idleness
  and closes new admission without sealing Sessions. A busy Runtime refuses; `ResumeAdmission`
  invalidates the attempt before dispatch reopens. An acknowledged attempt progresses through
  `Checkpointing` and `Resuming`, retaining its identity across lost replies and controller restarts.
  Even a concurrent wake waits for an actual checkpoint before thaw. Only after the launcher reports
  running does `ResumeAdmission` release the Runtime gate and the controller mark the attempt
  `Released`; the generation remains to distinguish delayed calls from the next attempt.
  A runner that is busy again, or idle since another instant, refuses, and the wait
  starts over; a controller restart also restarts it. Terminals and background processes do not
  count as work. Idleness never deletes a Sandbox.
- `status.idleSuspendAt` reports the next suspension attempt time while this controller has seen an
  idle runner and `spec.idleSuspendAfter` is set. It uses the controller's clock, starts when the
  controller first saw the runner's current idle period, and moves to the retry deadline after a
  refused or failed quiesce attempt. It is absent while suspension is off, the runner is busy or
  absent, or the Sandbox is stopping, stopped, deleting or blocked. A controller restart restarts
  the idle timer and reports its recomputed deadline.
- `status.idleSuspendBlockers` reports the current reasons the connected runner is busy: `ActiveTurn`,
  `PendingCommand`, `Setup` or `Activation`, each with its `sessionId`; `Bootstrap` and `Admission` have
  no Session. It is empty when autosuspend is off or no current reason is reported. The controller publishes these only from
  a live Runner Gateway Lease for the current incarnation, and clears them when that connection is
  absent, expired or belongs to another incarnation.
- A wake that lands while a Pod or VM idle suspension is being recorded replaces the incarnation:
  that runner is already sealed to new work. A Firecracker wake resumes the same incarnation.
- Each incarnation reads `spec.template` and the template as they are when it starts. Editing
  either never changes a running Pod or VM, its kind included.
- Runner control always uses the runner as an mTLS server and the Runner Gateway as its client. The
  controller exposes no transport choice on a `SandboxTemplate` and labels workloads only with the
  fresh credential ID used to bind each incarnation to its certificate.
- Before starting a new workload, the controller creates a cert-manager `Certificate` named
  `runner-<credential-id>` and waits for its generated TLS Secret. The leaf URI SAN is exactly
  `spiffe://agentplane/environment-runtime/<namespace>/<Sandbox UID>/<credential-id>` and has
  digital-signature, server-authentication and client-authentication usages. Server auth serves
  Runner control; client auth lets the egress sidecar authenticate to the separate mTLS proxy
  listener. The namespace Issuer signs it; cert-manager owns key generation and renewal. Runner TLS
  files mount from that generation's unique Secret, and the shared trust-manager CA bundle supplies
  the root. A renewed key can update only its own generation's Secret. Reconciliation upgrades the
  usages of a previously issued controller-owned Certificate only when every other identity and
  key-policy field still matches; SAN, issuer or other drift remains a conflict.
- A Pod projects the same per-incarnation Secret and trust bundle to the Environment Runtime and,
  when the template includes it, the `egress-sidecar` container. Runtime-only custom templates remain
  valid. The workload and executor containers receive no mount. The
  sidecar uses those files when `proxy.transport` selects TLS with `auth.mode: spiffe`; a
  handshake or certificate failure ends the request. It also sends the Kubernetes-audience token as destination context, reviewed against the
  live Sandbox Pod; that token never authenticates an SVID hop.
- A Pod UID is bound to its credential before the controller publishes its incarnation. A VM's
  first VMI UID is persisted on its VM and in the Sandbox's status. If KubeVirt unexpectedly replaces a VMI while
  the VM still carries the old identity, the controller halts that replacement, rotates to a new
  credential ID and Secret name, and resumes only after the old VMI is gone and the new certificate
  is ready. A replacement never inherits a credential issued to its predecessor.
- `status.runnerCredential` is the Sandbox's binding: the current credential ID and workload kind,
  the bound Pod UID, and the last accepted VMI UID and credential ID. Only the controller writes it,
  through the status subresource, so a writer of the Sandbox's spec or metadata cannot rebind it.
- An older running Pod or VM without a runner credential follows the normal quiesce and stop path;
  the controller does not halt an active VM just to install credentials. A coordinated Gateway
  authentication rollout must wait until legacy runners are stopped.
- Each Pod and VM carries the spec rendered from the template when it started
  (`agentplane.allegedly.works/rendered-spec`), for diffing against what the template renders now.
- A template that does not exist, sets none or multiple kinds, has an incompatible workspace layout, or
  defines the state volume or its workspace volume itself, starts nothing.

## VMs

- When the cluster does not serve KubeVirt's API, a VM Sandbox reports `Running=False` with reason
  `KubeVirtMissing` and creates no VM. Pod Sandboxes remain available.
- A VM Sandbox has one VirtualMachine, named after it and controlled by it. While `spec.running`
  is set its run strategy is `Always`, so KubeVirt replaces a VMI that ends; clearing it sets
  `Halted`. Stopping keeps the VM and its disks.
- Each start of a halted VM sets its spec from the template as it is then; its disks stay.
- The guest gets the state PVC as a virtiofs filesystem named after the template's state volume.
- The runner TLS Secret is a separate virtiofs config volume under a root-only guest directory.
  The runner reads its certificate and private key there; the guest's unprivileged native-agent
  account cannot traverse that directory. The KubeVirt launcher relay receives a projection of this
  same Secret only after its admission policy checks the live Sandbox's `status.runnerCredential`
  against the VM annotation, current VMI UID and credential label, plus the existing Sandbox owner
  chain. The egress-sidecar alone mounts that projection; the guest Runtime uses the existing
  virtiofs mount. Kyverno's admission controller gets only namespace-scoped Sandbox `get` for
  the status lookup; the kubelet projects the Secret without Kyverno reading its value. Missing
  accepted credential data fails admission; KubeVirt retries after status is bound, without an
  admitted Pod waiting forever on a placeholder Secret.
  The trust-manager CA bundle is mounted separately as public trust data.
- The guest's executable erofs disk keeps serial `agentplane-runner` and mounts at
  `/run/agentplane/environment`. Runtime configuration and the launch file arrive as separate
  disks at `/run/agentplane/config` and `/run/agentplane/sandbox`. Their filenames, device serials
  and guest mounts are recorded in [`env/images/vm_guest_contract.json`](../env/images/vm_guest_contract.json);
  the guest image build asserts its evaluated mounts against that contract.
- Each of the template's DataVolume templates is named `<sandbox>-<name>`, and the template's
  volumes naming it follow. Those disks belong to the VM: they outlive stops and go with the VM.
- Optional `workspaceVolume` declares a retained workspace's name, claim and required layout:
  `DirectoryTree`, `RawDiskImage` or `BlockFilesystem`. Directory trees mount in Pods or reach VMs
  through virtiofs; disk layouts attach to VMs or Firecracker launchers and require explicit `ext4`.
  The layout and claim mode must agree. Pod and VM state PVCs remain `DirectoryTree`; Firecracker
  state PVCs use an ext4 `RawDiskImage`.
- The Sandbox owns `<sandbox>-workspace`, which survives suspension and compatible kind changes.
  The PVC's creation record persists its layout and claim declaration, and `status.volumes.workspace`
  pins its name, UID and layout. A restart recovers a recorded claim, never adopts unrecorded contents.
  A changed layout, incompatible attachment, conflicting identity or omission of a retained workspace
  refuses the next start with `VolumeLayoutMismatch`, before replacing a workload. No conversion or
  reformatting occurs. [Storage contract](../docs/sandbox_storage.md).
- A kind change cannot discard a retained VM's DataVolume disks. Custom VM templates without a
  managed workspace declaration continue to use their own DataVolumes across stop/start.
- The VM, its VMI and its virt-launcher Pod carry the Sandbox label. The controller does not set
  the launcher's ServiceAccount: KubeVirt sets one only from a volume the guest could read, so the
  namespace's launcher admission sets the Sandbox's.

## Firecracker

- The `firecracker` arm carries a launcher Pod template, immutable boot and runner image references,
  vCPU count, and memory size. It requires an ext4 `RawDiskImage` state PVC and a retained workspace
  using either ext4 `RawDiskImage` on a Filesystem claim or ext4 `BlockFilesystem` on a Block claim.
  An existing PVC with another layout is refused; the controller never reformats or converts it.
- The controller mounts the boot image at `/images/boot`, the runner image at `/images/runner`, and
  the state PVC at `/var/lib/agentplane/state` in only the `environment-runtime` launcher container.
  It mounts a raw workspace at `/var/lib/agentplane/workspace`, or exposes a block workspace at
  `/dev/agentplane-workspace`. The template supplies the other containers and network mounts.
- The Pod annotation `agentplane.allegedly.works/firecracker-running` is projected as
  `/run/agentplane/firecracker-intent/running`. The launcher alone owns guest freeze, checkpoint,
  kill, restore, and resume. The controller changes this intent through the Kubernetes API and
  reads the launcher's `GET :9092/state` through the read-only `pods/proxy` API. A status response
  is accepted only when its `pod_uid` matches the current launcher Pod UID. Unknown, failed, stale,
  or unavailable status never marks a suspend complete or a resume ready.
- Suspension keeps the same Pod, Pod UID, ServiceAccount, and runner credential. It does not quiesce
  the runner or seal Sessions. `status.stopStage` becomes `Stopped` only after the supervisor reports
  `suspended`; resuming waits for `running`. The launcher Pod also records the Gateway Lease epoch
  at suspension. Its old idle fact cannot trigger another autosuspend after wake: a newer connection
  epoch and a fresh full idle timer are required. A missing or ended launcher is held for recovery rather
  than cold booted on top of a possible checkpoint. Final deletion wakes a suspended launcher,
  quiesces the runner normally, and then deletes the Pod and retained claims.
- The new template arm is an API addition. Existing staging templates remain Pod/VM until changed;
  rollback before enabling Firecracker templates leaves them untouched. Once a Firecracker Sandbox
  has checkpointed state, rollback must preserve its launcher and PVC until a controller that
  understands the Firecracker arm can resume or delete it safely.

## Identity

- Every Sandbox gets a ServiceAccount of its name, which its Pods run as, replacing the template's.
- While `spec.policySets` names PolicySets, the Sandbox has one `PolicyBinding` of its name binding
  that account to exactly those sets, with no expiry. Clearing the list deletes it. Other bindings
  for the same account, such as runtime grants, are left alone.
- Both are owned by the Sandbox, so deleting the Sandbox revokes them.
- A Pod does not wait for the binding's readers to apply it: a grant that lands after the Pod
  starts only means its early requests are refused.
- A ServiceAccount or `PolicyBinding` of the Sandbox's name that it does not own starts nothing.

## Control Service

Every Sandbox has a headless, selector-less `<sandbox>-control` Service on the Runner Gateway's
public proxy port, 8080. Callers use their workload bearers to reach the connection holder through
this Service. The Gateway connects directly to the current workload's mTLS runner listener on port 9090. The controller repairs the per-Sandbox Service's managed fields; the Gateway owns its EndpointSlice.
A foreign same-name Service blocks new workload starts.

## Launch

- Every Sandbox gets a ConfigMap `<sandbox>-launch`, owned by it, holding its namespace, name, UID
  and `spec.bootstrap` as `launch.json`. A Pod mounts it read-only into its `environment-runtime` container; a VM
  gets it as a virtio disk of serial `agentplane-launch`. A template without a `runner` container,
  or that defines the launch volume itself, starts nothing.
- A changed `spec.bootstrap` reaches the Sandbox's next incarnation only.
- A ConfigMap of that name that the Sandbox does not own starts nothing.

## Deletion

- A deleted Sandbox with a state PVC of its own keeps its Pod or VM until its `Quiesced`
  condition is True. It is set once the runner has sealed every Session and the ingress has
  acknowledged each seal, so history holds every Session through its seal before its state goes.
  A stopped Sandbox runs once more to seal only if the runner that last connected is not the one
  recorded in `status.quiescedIncarnation`; one sealed when it was suspended is deleted as it is.
- The current validated incarnation stays published while it seals, including during certificate
  renewal, so the Gateway can continue reporting quiesce progress. A replacement must bind its own
  credential before publication. A restart needed for sealing uses the same certificate readiness
  gate and credential rotation as an ordinary start; deletion never reuses the previous workload's key.
- Then the Pod or VM is deleted, then, once it and any VMI are gone, the controller removes old
  runner Certificate resources and their cert-manager-generated TLS Secrets. The finalizer clears
  and the PVC goes with the Sandbox only after that cleanup finishes.
- A Sandbox without a state PVC of its own never had a runner, so nothing is sealed.
- Nothing forces a seal: a Sandbox whose seal cannot complete is cleared by removing its finalizer by hand.
- The Pod or VM and the Sandbox's ServiceAccount are owned by the Sandbox with `blockOwnerDeletion`, so
  foreground propagation and namespace deletion remove them before the seal completes and the
  unsealed tail of a Session's journal is lost; background propagation (the `kubectl` and
  `DeleteSandbox` default) keeps them until the finalizer clears.

## Status

`status.incarnation` names the Pod the runner runs in and its UID: the Sandbox's Pod, or its VMI's
virt-launcher Pod. The `Running` condition is True while the Pod runs (`PodRunning`) or the VMI runs
(`VMRunning`). Otherwise it is False, with reason `Stopped`, `Starting`, `PullingImage`, `Stopping`, `PodEnded`,
`VMEnded`, `SuspendPending`, `Quiescing`, `Deleting`,
`TemplateNotFound`, `TemplateInvalid`, `StateVolumeConflict`, `VolumeLayoutMismatch`,
`RunnerCredentialMissing`, `RunnerCredentialRequired`,
`RunnerCredentialPending`, `RunnerCredentialMismatch`, `RunnerCredentialCleanupPending`,
`RunnerCertificateIssuerMissing`, `CertificatePending`, `CertificateConflict`,
`CertificateIdentityConflict`, `CertificateSecretPending`, `CertificateSecretConflict`,
`VMIStopping`, `IncarnationUnpublished`, `ServiceAccountConflict`, `PolicyBindingConflict`,
`PodConflict` or `VMConflict`
(a Pod or VM of the Sandbox's name that it does not control). Reasons are display only; readers
dispatch on typed status fields.

`status.stopStage` is how far the controller has taken stopping the workload, absent while it runs or
starts: `Quiescing` while it seals the current incarnation's Sessions before stopping or deleting it,
`Deleting` while a deleted Sandbox with nothing left to seal has its workload and runner credentials
removed, and `Stopped` once no plain Pod or VM runs, or a Firecracker launcher has checkpointed RAM
while keeping its Pod, and the state volume is kept. Credential and certificate pending
reasons requeue while cert-manager provisions or cleans up generation-specific resources; conflicts
keep the workload from starting until resolved. `SuspendPending` holds a suspend while the runner's bootstrap
or a Session's setup is still running, so the suspend never interrupts it; a delete does not wait.
This hold has `status.operatingMode: Suspended` with no stop stage. When the hold ends, the controller
publishes `Quiescing` with the attempt before its Quiesce RPC. The Gateway admits that RPC from the
controller through an informer snapshot that still shows the held suspend, but denies other Runner
proxy methods there; incarnation, Pod readiness, connection epoch, and caller checks still apply.
While a scheduled Pod is Pending, the reason is
`PullingImage` when its latest Event is an image pull, and the message carries that latest Event;
when no Event can be read the message says only that the Pod is not running yet.

`WorkloadReady` is True while the Sandbox runs and the incarnation Pod is Ready (`PodReady`).
Otherwise it is False with `PodNotReady` when everything runs but the Pod is not Ready, and else
with the reason and message of `Running`.

`EnvironmentRuntimeConnected` is True while the Runner Gateway's connection Lease for the Sandbox names the
incarnation's Pod and was renewed within its duration plus a grace, so a connection from an earlier
incarnation never counts and a gateway rollout's brief drop does not. It is False with `NeverConnected`,
`AwaitingEnvironmentRuntime`, `ConnectionLost`, `ConnectionPending` or `WorkloadNotReady`. `ConnectionPending`
means the Gateway has claimed observation of this incarnation but has not received its first valid
`WatchState` message containing the runner boot ID. The Gateway initiates the mTLS connection to the
runner listener; the claim precedes dialing and does not prove connectivity or authentication. Losing the connection restarts and replaces nothing. A controller not configured to
read Leases omits it.

Suspension and deletion require quiescence only for a verified runner incarnation. A Gateway
observation claim alone does not prove contact. The Lease retains `last-connected-pod-uid` after
a validated boot identity is recorded, across reconnect claims and Lease expiry; losing connectivity
does not waive quiescence for a previously connected runner. A pending claim with no verified contact
can stop without a Quiesce RPC.

`Bootstrapped` is True once the Sandbox's bootstrap has run to success, and when the Sandbox has
none (`NoBootstrap`). It is Unknown with `BootstrapNotReported` while the incarnation's runner has
reported nothing, False with `BootstrapRunning` while the script runs, and False with
`BootstrapFailed` when it failed or was interrupted, which is terminal: the controller never
retries it. A controller not configured to read Leases
omits it.

`Ready` is True while `WorkloadReady` and `EnvironmentRuntimeConnected` are True and `Bootstrapped` is not
False (Unknown does not hold it), and otherwise False with the reason and message of the first
that does not. It means the Sandbox can take work; the lifecycle phase is separate.
