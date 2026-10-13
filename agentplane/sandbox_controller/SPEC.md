# Sandbox Controller

Reconciles Agentplane's own `Sandbox` (`agentplane/crds/manifests/crd-sandboxes.yaml`) into Pods or
a KubeVirt VirtualMachine, and the state PVC. A Sandbox is its state, running or not; an
**incarnation** is one Pod, or one run of the VM. Its `SandboxTemplate` sets its kind: `pod` or
`vm`, exactly one.

## State PVC

- Every Sandbox gets one PVC, `<sandbox>-state`, created from its `SandboxTemplate`'s claim when
  the Sandbox is created, whether or not it is running.
- The Sandbox is the PVC's controller owner, without `blockOwnerDeletion`, so the Pod going away or
  stopping never removes it, and foreground propagation or namespace deletion cannot remove it
  before the seal completes. A Sandbox never uses a PVC another Sandbox controls, and reports
  `StateVolumeConflict` instead.
- Deleting the Sandbox removes its PVC by garbage collection once its Sessions are sealed, its Pod or
  VM is gone and the finalizer clears (below). Stopping never does.

## Incarnations

- While `spec.running` is set, the Sandbox has exactly one Pod, named after it and controlled by
  it, with the template's state volume backed by the PVC. A Pod that goes away or ends while the
  Sandbox is running is replaced by a new incarnation on the same PVC.
- Clearing `spec.running` deletes the Pod, once the runner that connected from it has sealed every
  Session (`status.quiescedIncarnation` names the sealed Pod). A Pod whose runner never connected has
  nothing to seal. The Sandbox stays held, with `Running` reason `Sealing`, while the seal fails or
  the ingress has not acknowledged it. Applies only to a controller configured to seal.
- A resumed Sandbox whose Pod is already sealed gets a new incarnation, as a sealed runner takes no
  new Sessions.
- Each incarnation reads `spec.template` and the template as they are when it starts. Editing
  either never changes a running Pod or VM, its kind included.
- Each Pod and VM carries the spec rendered from the template when it started
  (`agentplane.allegedly.works/rendered-spec`), for diffing against what the template renders now.
- A template that does not exist, sets neither or both kinds, or defines the state volume itself,
  starts nothing.

## VMs

- A VM Sandbox has one VirtualMachine, named after it and controlled by it. While `spec.running`
  is set its run strategy is `Always`, so KubeVirt replaces a VMI that ends; clearing it sets
  `Halted`. Stopping keeps the VM and its disks.
- Each start of a halted VM sets its spec from the template as it is then; its disks stay.
- The guest gets the state PVC as a virtiofs filesystem named after the template's state volume.
- Each of the template's DataVolume templates is named `<sandbox>-<name>`, and the template's
  volumes naming it follow. Those disks, the VM-local workspace among them, belong to the VM: they
  outlive stops and go with the VM.
- Starting a Sandbox whose template is now a Pod deletes its VM, and with it those disks, before
  the Pod starts; the state PVC stays.
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
  (A controller not configured to seal always runs it once more and waits for the gateway.)
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
(`VMRunning`). Otherwise it is False, with reason `Stopped`, `Starting`, `Stopping`, `PodEnded`,
`VMEnded`, `Sealing`, `Deleting`,
`TemplateNotFound`, `TemplateInvalid`, `StateVolumeConflict`, `ServiceAccountConflict`,
`PolicyBindingConflict`, `PodConflict` or `VMConflict` (a Pod or VM of the Sandbox's name that
it does not control).

`WorkloadReady` is True while the Sandbox runs and the incarnation Pod is Ready (`PodReady`).
Otherwise it is False with `PodNotReady` when everything runs but the Pod is not Ready, and else
with the reason and message of `Running`.

`RunnerConnected` is True while the Runner Gateway's connection Lease for the Sandbox names the
incarnation's Pod and was renewed within its duration plus a grace, so a connection from an earlier
incarnation never counts and a gateway rollout's brief drop does not. It is False with `NeverConnected`,
`AwaitingRunner`, `ConnectionLost` or `WorkloadNotReady`. Losing the connection restarts and replaces
nothing. A controller not configured to read Leases omits it.

`Ready` is True while both hold, and otherwise False with the reason and message of the first that
does not. It means the Sandbox can take work; the lifecycle phase is separate.
