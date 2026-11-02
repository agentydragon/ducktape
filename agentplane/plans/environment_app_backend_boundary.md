# Environment management: app/backend boundary

The eventual goal is to retire `app/go/sandboxes` as an app-owned domain backend as its callers
move to their owning services. The app remains responsible for browser authentication, presentation
and orchestration; backends own shared resource behavior and must operate without the app. Use
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
  attribution, idempotency and unknown-outcome handling when extracting the app's implementation.
- **Console:** Decide whether the KubeVirt console connector becomes a shared backend capability
  or remains an app-specific transport. A browser-facing streaming adapter may remain in the app.

Move or remove package-specific errors with their actual owners as callers migrate. The app's
handwritten `SandboxView` can be retired when its remaining routes and consumers use the generated
contracts. These later ownership decisions do not gate the current Discovery cutover.

## TODO: Remove the redundant frontend Sandbox view

The app cutover in #13878 retains `sandboxView()` in `app/frontend/sandboxes/live.tsx`: it converts
the generated `Sandbox` protobuf into the app's OpenAPI `SandboxView` for UI consumers. Inventory
test fixtures now use generated `Sandbox` messages and shared fixture builders (#13899, #13905). This
TODO now concerns the remaining production consumers of the app `SandboxView`.

The inventory UI migration remains open: move its consumers from `SandboxView` to generated
`Sandbox` messages. Move remaining detail and mutation consumers to the owning generated contracts,
then remove the redundant app `SandboxView` and production projection. Keep actual presentation
derivations such as status labels local to the UI. The detail contract and the relationship between
`Sandbox` and `EnvironmentDescriptor` still need to be resolved; this TODO does not select their
API shape or change caller authorization.
