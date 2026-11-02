# Environment management: app/backend boundary

The app still owns Sandbox HTTP routes and invokes the shared `sandboxes/inventory` package
directly. The app remains responsible for browser authentication, presentation and orchestration;
backends own shared resource behavior and must operate without the app. Move the app's callers to
protobuf-backed service contracts for shared resource data and operations.

- **Inventory and discovery:** Move app list/get, detail streams and their consumers to the
  appropriate backend contracts. The collection watch proposed in #13864 and its app/frontend
  cutover in #13878 address one stream, not the remaining readers. Reconcile the collection's
  `Sandbox` protobuf with `EnvironmentDescriptor` list/get, operator diagnostics and exact-UID
  reads; preserve caller authorization. Diagnostics ownership remains open.
- **Lifecycle and configuration:** The selected
  [resume backend](task_dag.md#environment_lifecycle_backend--own-resume-policy-and-guarded-state-changes)
  owns shared authorization and guarded mutation, initially hosted in the Gateway. The Sandbox
  Controller reconciles desired state into Pod/VM transitions. Suspend, delete and wake/idle
  configuration need separately authorized backend contracts; preserve UID/resource-version
  fences and retry semantics. The service's eventual deployment remains open.
- **Creation and templates:** The [environment management](task_dag.md#environment_management--dedicated-environment-tools-and-creation)
  and catalogue decisions own provisioning and template placement. Preserve creation's caller
  attribution, idempotency and unknown-outcome handling as the app's callers move to that owner.
- **Console:** Decide whether the KubeVirt console connector becomes a shared backend capability
  or remains an app-specific transport. A browser-facing streaming adapter may remain in the app.

Inventory errors live with the shared Kubernetes owner; app-specific response errors remain with
their route owner. The app's handwritten `SandboxView` can be retired when its remaining routes and
consumers use the generated contracts. These later ownership decisions do not gate the current
Discovery cutover.

## TODO: Remove the redundant frontend Sandbox view

The #13878 app cutover introduced the generated `Sandbox` protobuf for inventory data while the
app's OpenAPI `SandboxView` remains part of its UI contract, leaving overlapping representations to
reconcile. Test fixtures now use generated `Sandbox` messages and shared fixture builders (#13899,
#13905); the fixture conversion work is complete.

Migrate any remaining inventory consumers to generated `Sandbox` messages. Move remaining detail
and mutation consumers to the owning generated contracts, then remove the redundant app `SandboxView`
and production projection. Keep actual presentation derivations such as status labels local to the UI.
The detail contract and relationship between `Sandbox` and `EnvironmentDescriptor` still need to be
resolved; this TODO does not select their API shape or change caller authorization.
