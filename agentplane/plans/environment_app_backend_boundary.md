# Environment management: app/backend boundary

The eventual goal is to retire `app/go/sandboxes` as an app-owned domain backend as its callers
move to their owning services. The app remains responsible for browser authentication, presentation
and orchestration; backends own shared resource behavior and must operate without the app. Use
protobuf-backed service contracts for shared resource data and operations.

- **Inventory and discovery:** EnvironmentService owns the shared `EnvironmentDescriptor` for
  exact-UID gets, lists and collection watches, including the app's diagnostic fields. The app
  composes that data with its threads through separate app RPCs; the ownership of policy details
  remains a separate decision.
- **Lifecycle and configuration:** The selected
  [resume backend](task_dag.md#environment_lifecycle_backend--own-resume-policy-and-guarded-state-changes)
  owns shared authorization and guarded mutation. EnvironmentService runs in the existing Runner
  Gateway binary and shares its lifecycle manager. The Sandbox Controller reconciles desired state
  into Pod/VM transitions. Suspend, delete and wake/idle configuration use separately authorized
  backend contracts with UID/resource-version fences and preserved retry semantics.
- **Creation and templates:** The [environment management](task_dag.md#environment_management--dedicated-environment-tools-and-creation)
  and catalogue decisions own provisioning and template placement. EnvironmentService owns creation
  and template listing, including caller attribution, idempotency and unknown-outcome handling.
- **Console:** Decide whether the KubeVirt console connector becomes a shared backend capability
  or remains an app-specific transport. A browser-facing streaming adapter may remain in the app.

Move or remove package-specific errors with their actual owners as callers migrate. Presentation
derivations such as status labels remain local to the UI.
