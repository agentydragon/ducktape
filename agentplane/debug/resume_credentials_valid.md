# Credentials across Sandbox suspension

Audited 2026-10-15 against the Sandbox Controller, Policy Controller, app grant API, egress proxy,
and rendered Pod and VM templates. This is a code-path review; it makes no cluster claim.

The Sandbox Controller's `PolicyBinding` comes from `Sandbox.spec.policySets`, is owned by the
Sandbox, and omits `expiresAt` (`sandbox_controller/identity.go`). Its existing test asserts that
shape (`sandbox_controller/identity_test.go::TestBindingIsCreatedWithThePodWithoutWaitingForItsReaders`).
It remains the Sandbox's non-expiring configured grant across a stop and resume.

The app's operator-authenticated `POST /{name}/policy-bindings` creates a new
`PolicyBinding` for the named Sandbox. It may set `expiresAt`; a later grant creates another binding
rather than editing or extending the previous grant (`app/api.py`, `app/egress_access.py`). The
Policy Controller schedules a reconcile at the deadline, deletes the binding, and removes the native
RBAC grants on its following NotFound reconcile. The deterministic test is
`policy_controller/controller_test.go::TestExpiredBindingIsDeletedAndItsGrantsWithIt`. There is no
resume refresh path. Revocation also deletes the binding. Since these grants are separate and
additive, a Sandbox may still have other grants after one expires.

The egress proxy watches current `PolicyBinding` objects. It returns `no-binding` when the
ServiceAccount has no extant egress grant, and `no-rule` when those grants have no matching rule
(`egress/policy.py`). The app's egress projection carries `expiresAt` for each extant grant but does
not filter deadline-passed objects before controller deletion (`sandboxes/egress_views.py`).
Sandbox `Ready` is computed from workload readiness, runner connection and bootstrap; it does not
wait for app-created bindings or their reader conditions (`sandbox_controller/controller.go`).
No object records that an app-created binding is required for a Sandbox to be considered ready.

Expiry is controller-driven: `policy_sets.py` says readers treat an existing binding as a grant. The
egress reader does not compare `expiresAt` to its clock. The existing test establishes deletion
after an expired reconcile, not a strict authorization cutoff if controller reconciliation or watch
propagation is delayed. This audit found no resume path in these controllers that renews or recreates
app-created runtime grants. Whether expiry must be strict at every reader is a separate enforcement
decision.

`EgressCredential` has no expiry field. Its source is either a Kubernetes Secret, the authenticated
hop token, or a projected token (`egress/resources.py`). Secret-backed value lifetime and rotation
belong to the Secret's writer; Agentplane has no credential refresh contract for an upstream
expiry it cannot observe.

For Pod Sandboxes, the egress sidecar alone mounts the Pod-bound projected hop and audience tokens.
Each uses a 600-second lifetime; kubelet rotates them, and the sidecar rereads the files on every
request (`cluster/cdk8s/agentplane/sandbox_pod.py`, `egress/token_file.py`; [Kubernetes token
projection docs](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/)).
Resume creates a new Pod from the current template (`sandbox_controller/controller_test.go::TestSuspendedThenResumedPodRunsTheCurrentTemplateAndRecordsItsSpec`),
so kubelet issues projections for the new Pod. VM guests receive no Kubernetes token; admission
injects the same projected-token volume only into the new virt-launcher Pod's egress relay
(`cluster/cdk8s/agentplane/launcher_relay.py`, `sandbox_controller/vm.go`). TokenReview checks the
token's current audience and bound Pod identity, so an old or invalid token is rejected
(`workload_auth/principal.py`).

Existing deterministic tests cover these boundaries separately. Policy expiry deletes the binding
and its native grants after an expired reconcile; egress without an extant binding denies; rendered
Sandbox templates project the configured audiences; the sidecar rereads a rotated token; and Sandbox
resume rerenders the workload. A fake kubelet test would not establish that a real projected token
is fresh, so this audit adds no duplicate test. No live-cluster token test is needed absent a specific
deployment uncertainty.

Decision requested: should app-created runtime bindings remain optional additive capabilities
(recommended; current object model), or become prerequisites for a useful resumed Sandbox? The
required-grant option needs durable identification of required policy sets, a decision whether the
gate blocks creation, readiness or dispatch, and an operator path to issue a replacement grant. In
either option, expiry and revocation remain authoritative: do not renew or recreate authority
implicitly. Separately decide whether `expiresAt` requires a strict reader-side wall-clock check, or
whether controller deletion is the intended expiry boundary.
