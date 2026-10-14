# Sandboxes

The shared library for Sandbox requesters and readers: the integration app, Runner Gateway and
Notification Service. Kubernetes holds all Sandbox state. The app submits desired state and operator
grants; the Sandbox Controller reconciles them and owns Sandbox status. `SandboxReader`
reads a Sandbox and its Pod; `SandboxInventory` also creates (idempotently, by a recorded create intent),
guards `spec.running` patches by UID and resourceVersion, refuses resume with a retryable error while the
controller is quiescing an incarnation, and starts deletion of a suspended Sandbox;
`kubernetes_views` derives a Sandbox's phase from its status.

`testing/kubernetes.py` provides a local API server and a seeded `SandboxCluster` for integration tests.
