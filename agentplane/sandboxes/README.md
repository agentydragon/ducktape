# Sandboxes

Shared Kubernetes types and exact-identity readers for Sandbox controllers and backend services.
Kubernetes holds all Sandbox state. The Runner Gateway's `lifecycle` manager
authorizes its explicit-resume API and automatic wake, applying guarded `spec.running`/`wakeGeneration`
updates; operator authority and ordinary execution-account policy are checked at request time. The
Dispatcher DeliveryWorker requests automatic wake through the shared manager, preserving the effective
caller and applying `wakeOnSend` and the configured `wake_callers` grant. The Sandbox Controller
reconciles desired state and owns Sandbox status. The shared `sandboxreader` reads exact Sandbox identities and execution accounts, while
the Gateway owns the canonical `EnvironmentDescriptor` projection and its get/list/watch API. The Gateway's
private EnvironmentService management package owns template listing, idempotent creation and
UID/resourceVersion-fenced suspension, deletion and policy updates. Explicit resume and send-wake
requests use the Gateway lifecycle manager, rereading current state on resourceVersion conflicts;
concurrent wake requests advance one `wakeGeneration`.

`spec.resumePolicy.allowOwnServiceAccount` opts the exact live execution ServiceAccount UID into explicit
resume of idle suspension; `spec.resumePolicy.allowOwnIdentity` does the same for the live Identity
`spec.identity` names. `spec.wakeOnSend` and the existing `wake_callers` setting continue to
govern automatic delivery wake. A separate optional direct Pod-bound `operator_callers` grant lets
an operator explicitly resume a requested stop; delegated callers cannot use that grant.

`status.operatingMode` is the controller's effective-mode report. It is absent until the controller
first reconciles a new Sandbox, so descriptors expose an unspecified mode and phase instead of
deriving actual state from `spec.running`. Deletion requires an explicitly reported `Suspended` mode;
send-triggered wake work stays queued without a wake deadline until a mode is reported.

`status.idleSuspendBlockers` carries the live runner reasons that prevent autosuspension. Inventory
readers project it as `EnvironmentDescriptor.observed_state.autosuspend_blockers`, preserving reasons and
nullable Session IDs.

`spec.inboxTags` are plain strings with no backing object; a `session_inbox_access` rule targeting
one covers every Sandbox holding it ([messages](../notification_service/docs/api.md#messages)).
Sandbox RBAC is the only gate, with no admission rule: whoever can patch a Sandbox can already set
its `spec.policySets`, which grants at least as much. Flux and the app both write tags. Server-side
apply owns each entry of the set-typed list separately, so on a Sandbox that Flux creates, Flux and
the app can each own different tags, and Flux reverts a tag it declares if someone changes it live.
Flux cannot tag a Sandbox the app created, because it would then own the whole object. A recreated
Sandbox has only the tags its manifest or launch gives it.

`testing/kubernetes.py` seeds a running Sandbox and its Pod (`SandboxCluster`) on an envtest API
server for integration tests. `testing/sandbox_namespace.py` provides the shared Kubernetes fixture
operations.
