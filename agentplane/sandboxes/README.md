# Sandboxes

The shared library for Sandbox requesters and readers: the integration app, Runner Gateway and
Notification Service. Kubernetes holds all Sandbox state. The app submits desired state and operator
grants; the Sandbox Controller reconciles them and owns Sandbox status. `SandboxReader`
reads a Sandbox and its Pod; `SandboxInventory` also creates (idempotently, by a recorded create intent),
guards `spec.running` patches by UID and resourceVersion, refuses resume with a retryable error while the
controller is quiescing an incarnation, and starts deletion of a suspended Sandbox. On a resourceVersion
conflict, resume and send-wake requests reread and re-evaluate current state; concurrent wake requests
advance one `wakeGeneration`. `kubernetes_views` derives a Sandbox's phase from its status.

`status.operatingMode` is the controller's effective-mode report. It is absent until the controller
first reconciles a new Sandbox, so readers expose a nullable mode and `Unspecified` phase instead of
deriving actual state from `spec.running`. Deletion requires an explicitly reported `Suspended` mode;
send-triggered wake work stays queued without a wake deadline until a mode is reported.

`status.idleSuspendBlockers` carries the live runner reasons that prevent autosuspension. Inventory
readers project it as `Sandbox.autosuspend_blockers`; the integration app's `SandboxView` includes the
same reasons and nullable Session IDs.

`spec.inboxTags` are plain strings with no backing object; a `session_inbox_access` rule targeting
one covers every Sandbox holding it ([messages](../notification_service/docs/api.md#messages)).
Sandbox RBAC is the only gate, with no admission rule: whoever can patch a Sandbox can already set
its `spec.policySets`, which grants at least as much. Flux and the app both write tags. Server-side
apply owns each entry of the set-typed list separately, so on a Sandbox that Flux creates, Flux and
the app can each own different tags, and Flux reverts a tag it declares if someone changes it live.
Flux cannot tag a Sandbox the app created, because it would then own the whole object. A recreated
Sandbox has only the tags its manifest or launch gives it.

`testing/kubernetes.py` seeds a running Sandbox and its Pod (`SandboxCluster`) on an envtest API
server for integration tests.
