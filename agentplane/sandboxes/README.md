# Sandboxes

Shared Kubernetes types and exact-identity readers for Sandbox controllers and backend services.
Kubernetes holds all Sandbox state. [`environments/lifecycle`](../environments/lifecycle/README.md)
authorizes explicit resume (served by Environments) and automatic wake (executed by the Dispatcher),
applying guarded `spec.running`/`wakeGeneration` updates; operator, Identity and broker authority are
checked at request time. The Dispatcher DeliveryWorker requests automatic wake through its wake
manager, preserving the effective caller and applying `wakeOnSend` and the configured
`wake_service_callers` grant. The Sandbox Controller reconciles desired state and owns Sandbox status. The shared
`sandboxreader` reads exact Sandbox identities and execution accounts, while Environments'
EnvironmentService owns the canonical `EnvironmentDescriptor` projection and its get/list/watch API.
The EnvironmentService's private management package owns template listing, idempotent creation and
UID/resourceVersion-fenced suspension, deletion and policy updates. Explicit resume and send-wake
requests share the guarded write loop, rereading current state on resourceVersion conflicts;
concurrent wake requests advance one `wakeGeneration`.

`spec.resumePolicy.allowOwnServiceAccount` opts the exact live Identity the Sandbox acts as into
automatic wake of idle suspension, never explicit resume. `spec.wakeOnSend` and the
`wake_service_callers` setting govern automatic delivery wake. A separate `service_operators` grant
lets an operator service explicitly resume a requested stop; delegated callers cannot use that grant.

`status.operatingMode` is the controller's effective-mode report. It is absent until the controller
first reconciles a new Sandbox, so descriptors expose an unspecified mode and phase instead of
deriving actual state from `spec.running`. Deletion requires an explicitly reported `Suspended` mode;
send-triggered wake work stays queued without a wake deadline until a mode is reported.

`status.idleSuspendBlockers` carries the live runner reasons that prevent autosuspension. Inventory
readers project it as `EnvironmentDescriptor.observed_state.autosuspend_blockers`, preserving reasons and
nullable Session IDs.

`spec.inboxTags` are plain strings with no backing object; a `session_inbox_access` rule targeting
one covers every Sandbox holding it ([messages](../notification_service/docs/api.md#messages)).
Sandbox RBAC controls who can change those tags; Flux and the app both write them. Server-side apply
owns each entry of the set-typed list separately, so on a Sandbox that Flux creates, Flux and the
app can each own different tags, and Flux reverts a tag it declares if someone changes it live. Flux
cannot tag a Sandbox the app created, because it would then own the whole object. A recreated
Sandbox has only the tags its manifest or launch gives it.

`testing/kubernetes.py` seeds a running Sandbox and its Pod (`SandboxCluster`) on an envtest API
server for integration tests. `testing/sandbox_namespace.py` provides the shared Kubernetes fixture
operations.
