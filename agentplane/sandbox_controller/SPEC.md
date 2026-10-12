# Sandbox Controller

Reconciles Agentplane's own `Sandbox` (`agentplane/crds/manifests/crd-sandboxes.yaml`) into Pods
and the state PVC. A Sandbox is its state, running or not; an **incarnation** is one Pod.

## State PVC

- Every Sandbox gets one PVC, `<sandbox>-state`, created from its `SandboxTemplate`'s claim when
  the Sandbox is created, whether or not it is running.
- The PVC has no owner reference, so neither garbage collection nor the Pod going away removes it.
  It carries the Sandbox's UID; a Sandbox never uses or deletes a PVC another Sandbox created, and
  reports `StateVolumeConflict` instead.
- Deleting the Sandbox deletes its PVC once its Sessions are sealed and its Pod is gone (below).
  Stopping never does.

## Incarnations

- While `spec.running` is set, the Sandbox has exactly one Pod, named after it and controlled by
  it, with the template's state volume backed by the PVC. A Pod that goes away or ends while the
  Sandbox is running is replaced by a new incarnation on the same PVC.
- Clearing `spec.running` deletes the Pod.
- Each incarnation reads `spec.template` and the template as they are when it starts. Editing
  either never changes a running Pod.
- Replacing the runner image is editing the template, or pointing `spec.template` at another, while
  the Sandbox is stopped or between incarnations; the next Pod runs it on the same PVC.
- The PVC records the `stateSchemaVersion` of the template that created it. A template declaring
  another starts nothing on it.
- A template that does not exist, or that defines the state volume itself, starts nothing.

## Identity

- Every Sandbox gets a ServiceAccount of its name, which its Pods run as, replacing the template's.
- While `spec.policySets` names PolicySets, the Sandbox has one `PolicyBinding` of its name binding
  that account to exactly those sets, with no expiry. Clearing the list deletes it. Other bindings
  for the same account, such as runtime grants, are left alone.
- Both are owned by the Sandbox, so deleting the Sandbox revokes them.
- A Pod does not wait for the binding's readers to apply it: a grant that lands after the Pod
  starts only means its early requests are refused.
- A ServiceAccount or `PolicyBinding` of the Sandbox's name that it does not own starts nothing.

## Deletion

- A deleted Sandbox with a state PVC of its own keeps its Pod, and a stopped one runs once more,
  until its `SessionsSealed` condition is True. The Runner Gateway sets it once the runner has
  sealed every Session and the ingress has acknowledged each seal, so history holds every Session
  through its seal before its state goes.
- Then the Pod is deleted, then, once it is gone, the PVC, then the Sandbox.
- A Sandbox without a state PVC of its own never had a runner, so nothing is sealed.

## Status

`status.incarnation` names the current Pod and its UID. The `Running` condition is True while the
Pod runs. Otherwise it is False, with reason `Stopped`, `Starting`, `Stopping`, `PodEnded`,
`Sealing`, `Deleting`,
`TemplateNotFound`, `TemplateInvalid`, `StateVolumeConflict`, `StateSchemaMismatch`, `ServiceAccountConflict`,
`PolicyBindingConflict` or `PodConflict` (a Pod of the Sandbox's name that
it does not control).
