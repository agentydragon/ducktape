# Sandbox Controller

Reconciles Agentplane's own `Sandbox` (`agentplane/crds/manifests/crd-sandboxes.yaml`) into Pods
and the state PVC. A Sandbox is its state, running or not; an **incarnation** is one Pod.

## State PVC

- Every Sandbox gets one PVC, `<sandbox>-state`, created from its `SandboxTemplate`'s claim when
  the Sandbox is created, whether or not it is running.
- The PVC has no owner reference, so neither garbage collection nor the Pod going away removes it.
  It carries the Sandbox's UID; a Sandbox never uses or deletes a PVC another Sandbox created, and
  reports `StateVolumeConflict` instead.
- Deleting the Sandbox deletes its Pod, then, once the Pod is gone, its PVC. Stopping never does.

## Incarnations

- While `spec.running` is set, the Sandbox has exactly one Pod, named after it and controlled by
  it, with the template's state volume backed by the PVC. A Pod that goes away or ends while the
  Sandbox is running is replaced by a new incarnation on the same PVC.
- Clearing `spec.running` deletes the Pod.
- Each incarnation reads `spec.template` and the template as they are when it starts. Editing
  either never changes a running Pod.
- A template that does not exist, or that defines the state volume itself, starts nothing.

## Status

`status.incarnation` names the current Pod and its UID. The `Running` condition is True while the
Pod runs. Otherwise it is False, with reason `Stopped`, `Starting`, `Stopping`, `PodEnded`,
`TemplateNotFound`, `TemplateInvalid`, `StateVolumeConflict` or `PodConflict` (a Pod of the
Sandbox's name that it does not control).
