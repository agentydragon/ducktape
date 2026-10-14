# Sandboxes

The shared library every requester and reader of Sandbox resources uses: the Sandbox Service, the
Runner Gateway, the app and the Notification Service. Kubernetes holds all Sandbox state. `SandboxReader`
reads a Sandbox and its Pod; `SandboxInventory` also creates (idempotently, by a recorded create intent),
guards `spec.running` patches by UID and resourceVersion, and starts deletion of a suspended Sandbox;
`kubernetes_views` derives a Sandbox's phase from its status.
