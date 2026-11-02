# Environment management: app/backend boundary

EnvironmentService runs in Runner Gateway and owns environment inventory, exact-target reads,
watches, templates and management. The app calls its generated protobuf-backed API using its
service identity; the frontend consumes the generated Environment descriptor directly. The app
retains browser authentication, presentation and Thread and policy composition. The Sandbox
Controller reconciles desired state into Pod or VM transitions and owns observed status.

Creation retains caller attribution, idempotency and uncertain-outcome reconciliation. Management
uses UID and resource-version fences. Shared backend operations remain usable without the app.

## Console ownership decision

Decide whether the KubeVirt console connector becomes a shared backend capability or remains an
app-specific transport. A browser-facing streaming adapter may remain in the app. The existence of
an app-specific browser stream does not determine who owns backend console access.
