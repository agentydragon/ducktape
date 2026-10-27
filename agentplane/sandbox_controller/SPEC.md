# Sandbox Controller

Reconciles Agentplane's own `Sandbox` (`agentplane/crds/manifests/crd-sandboxes.yaml`) into Pods or
a KubeVirt VirtualMachine, and the state PVC. A Sandbox is its state, running or not; an
**incarnation** is one Pod, or one run of the VM. Its `SandboxTemplate` sets its kind: `pod` or
`vm`, exactly one.

## State PVC

- Every Sandbox gets one PVC, `<sandbox>-state`, created from its `SandboxTemplate`'s claim when
  the Sandbox is created, whether or not it is running.
- `stateVolume.layout` is required and accepts only `DirectoryTree` on a `Filesystem` claim.
  It records the layout on the PVC and pins the claim's name, UID and layout in
  `status.volumes.state` before attaching it. An existing PVC without a creation record is rejected, including a controller-owned claim.
  A missing or changed claim, foreign PVC, lost or unsupported record, or changed UID blocks workload
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
  `status.suspendedAtWakeGeneration`. Readers of `spec.running` read the mode instead.
- `status.suspendedBy` says why a Sandbox is suspended and is absent while it runs: `Requested` when
  `spec.running` is clear, `Idle` when the controller suspended it for idleness. A requester that
  clears `spec.running` of an idle-suspended Sandbox replaces `Idle` with `Requested`.
  `status.suspendedAtWakeGeneration` is set exactly with `Idle`.
- While `spec.running` is set, the Sandbox has exactly one Pod, named after it and controlled by
  it, with the template's state volume backed by the PVC. A Pod that goes away or ends while the
  Sandbox is running is replaced by a new incarnation on the same PVC.
- Clearing `spec.running` deletes the Pod, once the runner that connected from it has sealed every
  Session (`status.quiescedIncarnation` names the sealed Pod). A Pod whose runner never connected has
  nothing to seal. The Sandbox stays held, with `Running` reason `Quiescing`, while the quiesce fails or
  the ingress has not acknowledged it. The controller persists the last request time and runner Pod
  UID in `status.quiesceAttempt`, so watch events and controller restarts cannot bypass its one-minute
  retry deadline; Lease facts can complete the seal sooner. Applies only to a controller configured
  to seal.
- A resumed Sandbox whose Pod is already sealed gets a new incarnation, as a sealed runner takes no
  new Sessions.
- Optional `spec.idleSuspendAfter`, a duration of at least one minute, suspends an idle Sandbox. Unset,
  the controller never suspends it for idleness. Once the runner has been idle that long, timed on
  the controller's clock from when it first saw that idle period, the controller asks the runner to
  quiesce only if it is still idle since then, and the Sandbox is suspended as for a cleared
  `spec.running`. A runner that is busy again, or idle since another instant, refuses, and the wait
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
- A wake that lands while an idle suspension is being recorded replaces the incarnation: the runner is
  already sealed to new work, and the Sandbox is to run.
- Each incarnation reads `spec.template` and the template as they are when it starts. Editing
  either never changes a running Pod or VM, its kind included.
- Each Pod and VM carries the spec rendered from the template when it started
  (`agentplane.allegedly.works/rendered-spec`), for diffing against what the template renders now.
- A template that does not exist, sets neither or both kinds, sets a workspace volume without `vm`, or
  defines the state volume or its workspace volume itself, starts nothing.

## VMs

- When the cluster does not serve KubeVirt's API, a VM Sandbox reports `Running=False` with reason
  `KubeVirtMissing` and creates no VM. Pod Sandboxes remain available.
- A VM Sandbox has one VirtualMachine, named after it and controlled by it. While `spec.running`
  is set its run strategy is `Always`, so KubeVirt replaces a VMI that ends; clearing it sets
  `Halted`. Stopping keeps the VM and its disks.
- Each start of a halted VM sets its spec from the template as it is then; its disks stay.
- The guest gets the state PVC as a virtiofs filesystem named after the template's state volume.
- Each of the template's DataVolume templates is named `<sandbox>-<name>`, and the template's
  volumes naming it follow. Those disks belong to the VM: they outlive stops and go with the VM.
- Optional `workspaceVolume` declares a retained workspace's name, claim and required layout:
  `DirectoryTree`, `RawDiskImage` or `BlockFilesystem`. Directory trees mount in Pods or reach VMs
  through virtiofs; disk layouts attach only as VM disks and require explicit `ext4`. The layout
  and claim mode must agree. The state PVC remains `DirectoryTree` until a separate accepting code
  and template rollout supports another layout.
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

Every Sandbox has a headless, selector-less `<sandbox>-control` Service on the Runner Gateway
control port. The controller repairs its managed fields; the Gateway owns its EndpointSlice.
A foreign same-name Service blocks new workload starts.

## Launch

- Every Sandbox gets a ConfigMap `<sandbox>-launch`, owned by it, holding its namespace, name, UID
  and `spec.bootstrap` as `launch.json`. A Pod mounts it read-only into its `runner` container; a VM
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
- Then the Pod or VM is deleted, then, once it and any VMI are gone, the finalizer clears and the
  PVC goes with the Sandbox.
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
`TemplateNotFound`, `TemplateInvalid`, `StateVolumeConflict`, `VolumeLayoutMismatch`, `ServiceAccountConflict`,
`PolicyBindingConflict`, `PodConflict` or `VMConflict` (a Pod or VM of the Sandbox's name that
it does not control). `SuspendPending` holds a suspend while the runner's bootstrap is still running, so the
suspend never interrupts it; a delete does not wait. While a scheduled Pod is Pending, the reason is
`PullingImage` when its latest Event is an image pull, and the message carries that latest Event;
when no Event can be read the message says only that the Pod is not running yet.

`WorkloadReady` is True while the Sandbox runs and the incarnation Pod is Ready (`PodReady`).
Otherwise it is False with `PodNotReady` when everything runs but the Pod is not Ready, and else
with the reason and message of `Running`.

`RunnerConnected` is True while the Runner Gateway's connection Lease for the Sandbox names the
incarnation's Pod and was renewed within its duration plus a grace, so a connection from an earlier
incarnation never counts and a gateway rollout's brief drop does not. It is False with `NeverConnected`,
`AwaitingRunner`, `ConnectionLost` or `WorkloadNotReady`. Losing the connection restarts and replaces
nothing. A controller not configured to read Leases omits it.

`Bootstrapped` is True once the Sandbox's bootstrap has run to success, and when the Sandbox has
none (`NoBootstrap`). It is Unknown with `BootstrapNotReported` while the incarnation's runner has
reported nothing, False with `BootstrapRunning` while the script runs, and False with
`BootstrapFailed` when it failed or was interrupted, which is terminal: the controller never
retries it. A controller not configured to read Leases
omits it.

`Ready` is True while `WorkloadReady` and `RunnerConnected` are True and `Bootstrapped` is not
False (Unknown does not hold it), and otherwise False with the reason and message of the first
that does not. It means the Sandbox can take work; the lifecycle phase is separate.
