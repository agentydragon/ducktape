# Runtime proxy callers and the privileged boundary

Audit of devel `9f1fe4d2` (2026-11-15). **Disposition proposal for review; no traffic or
permission changes.** The operator released this audit after the EnvironmentService extraction.
The remaining proxy/Executor move is agreed but unreleased in [DAG section 17](task_dag.md#17-two-binaries-and-the-go-layout).

Recommend making the Runtime protocol a privileged backend surface owned by Environments.
Product clients use SessionDispatcher for Session operations and EnvironmentService for environment
operations. Retain exact-method transport access for Sessions delivery and the central sampler's
Executor, plus Controller-internal lifecycle control. This is a boundary recommendation, not new
authority for any peer. The existing [Session/environment model](session_environment_model.md)
allows zero or several attachments; no follow-up may turn a Sandbox into a required Session field.

## Scope and evidence

Searched the repository for generated `EnvironmentRuntime`/`Executor` client construction, their Go
and Python imports, `environmentruntimeclient.WithClient`, `SandboxControlClient`/`SandboxRunner`,
proxy target settings and method names; traced wrappers to production entry points and matched them
to receiver rules and cdk8s composition. Generated types, Runtime server implementations and shared
request types alone are not network callers. This is a source/configuration audit, not evidence of
staging traffic or a count of live clients.

The [Runtime protocol](../env/runtime/control/environment_runtime.proto) and
[Executor protocol](../env/runtime/execution/executor.proto) are distinct services on the Gateway's
shared listener. [The method gate](../session_dispatcher/internal/gateway/gateway.go#L118) admits no
public `WatchState` method and rejects delegation on proxy and Executor calls. Its four service
roles are independent. The [deployed composition](../../cluster/cdk8s/agentplane/session_dispatcher_workload.py#L330)
sets:

| Role                               | Exact client SVID                                              | Method grant                                                                                                                                              |
| ---------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runtime_proxy_readers`            | `spiffe://agentplane/app`, `spiffe://agentplane/notifications` | `ListSessions`, `GetStatus`                                                                                                                               |
| `runtime_proxy_session_callers`    | `spiffe://agentplane/sessions`                                 | `CreateSession`, `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn`                                                                   |
| `runtime_proxy_controller_callers` | `spiffe://agentplane/sandbox-controller`                       | `Quiesce`, `PrepareSuspend`, `ResumeAdmission`                                                                                                            |
| `executor_service_callers`         | `spiffe://agentplane/central-sampler` when enabled             | `OpenSession`, `StartProcess`, `ReadProcess`, `WriteProcess`, `SignalProcess`, `TerminateProcess`, `GetFileMetadata`, `OpenFile`, `ReadFile`, `CloseFile` |

These grants are broader than observed calls: Notifications can also `GetStatus`; App can also
`ListSessions`. No caller Identity is delegated through these methods. Component credentials admit
a trusted service, while destination headers/scope identify and fence the Sandbox; naming a Sandbox
does not authorize an arbitrary workload Identity. Legacy Gateway-audience bearer rules remain:
`gateway_callers` covers App, Notifications and the combined Gateway account for diagnostics;
Session writes admit the verified combined Gateway account, lifecycle calls the Controller account,
and Executor the sampler account. Their retirement belongs to
[auth stage 5](service_svid_stage5_readiness.md), not this audit. Do not widen those account grants
as services separate. Deployed and wire names remain unchanged.

## Callers and dispositions

### App: live diagnostics and archive safety

[Composition](../app/internal/server/runtime.go#L130) creates
[`GatewayEnvironmentRuntimeProxy`](../app/internal/sessionclient/environment_runtime.go), using the
App SVID. Active calls are `GetStatus`:

- [The browser Runtime-status route](../app/internal/httpapi/sessions/routes.go) calls
  [the Session handler](../app/internal/httpapi/sessions/handler.go#L139), which resolves an exact
  Environment through EnvironmentService before forwarding the live snapshot.
- [Thread archival](../app/internal/httpapi/threads/handler.go#L483) resolves the retained exact
  Environment and checks `GetStatus.sessions` for a busy Session. Missing exact Environment or no
  Pod permits archival; an existing unready Pod or unavailable status refuses it. This is a safety
  check, not merely a display.

**Disposition:** move raw process/boot/lifecycle inspection behind an explicit EnvironmentService
Runtime-diagnostics RPC, restricted to reviewed diagnostic peers. **This direction was approved by
the operator on 2026-11-15; the exact API and method grants remain implementation work.** The current
[EnvironmentService API](../environments/v1/environment_service.proto) exposes environment inventory
and lifecycle but not this full snapshot; its readiness descriptor is not a replacement for process
or per-Session busy state. Preserve the browser's response during the backend migration.

Move the archival decision onto the Session owner's current-status contract after
`SESSION_LIVE_STATUS_OWNERSHIP_DESIGN` settles freshness, unknown/stale states and the read path.
Do not equate a stale History summary, ready Sandbox or unreachable Runtime with an idle Session.
That existing design owns the persist-versus-live-read choice; this audit does not choose it.
Keep the current conservative check until its replacement exists.

The App's `SessionClient.ListSessions` and proxy wrapper have no production call site in this
baseline; current Session discovery already uses Dispatcher. Remove that unused App wrapper with
its consumer tests when narrowing the client, without deleting Runtime inventory needed by other
callers. Recheck references and selected plans then.

### Notifications: subscription-time materialization check

[`subscribe`](../notification_service/internal/rpc/subscribe.go#L26) first reads Dispatcher
`GetSession`, verifies the recorded execution Identity and exact destination against the caller,
and verifies the live Sandbox Identity. It then uses
[`ProxyRunner.ListSessions`](../notification_service/internal/rpc/runner.go#L25) under the
Notifications SVID, discarding every field except Session IDs. A missing Runtime record refuses the
subscription; an offline Runtime fails the request. History supplies outcome scans separately, and
notification delivery uses Dispatcher; neither goes through this Runtime inventory call.

**Disposition:** Notifications should use the Sessions authority for this validation and lose its
proxy role. `GetSession` already supplies retained admission/ownership, but does not prove that a
Runtime materialized the Session. Recommend accepting a subscription for an otherwise authorized,
admitted Session even when it is waiting for materialization or its Runtime is offline. Preserve
exact recorded Identity UID, live-caller and destination authorization and source authorization;
subscription creation must neither wake nor create a Session. Review this availability change
explicitly before removing the Runtime check. It grants no broader cross-Identity subscription or
new environment attachment authority. Broader environment-independent inbox/routing work remains
with [`CENTRAL_SESSION_NOTIFICATIONS`](task_dag.md#central_session_notifications--inboxes-for-sessions-without-environments).

That broader work is necessary even after removing this proxy call: `sessionidentity.Target`
currently refuses a Session without an Environment, subscription authorization compares that target
with the caller's Sandbox, and the stored inbox owner includes its UID. The
[delivery worker](../notification_service/internal/worker/destination.go) checks the stored Sandbox
and its execution Identity before [submitting notice input](../notification_service/internal/worker/deliver.go)
with `expected_sandbox_uid`. **Operator clarification, 2026-11-15:** Notifications must not require
an attachment for subscription, inbox ownership/access or notice delivery. A central provider-only
Session must receive notifications with zero attachments. The existing DAG node tracks the
Session-authority, actor-authentication, storage and routing changes and their preservation tests;
removing Runtime `ListSessions` alone does not implement this. Do not require a dummy Sandbox or
select a first attachment as notification authority.

If materialization is required instead, Sessions must expose an authorized materialization result
with unavailable/unknown semantics; do not replace it with “the Session exists in Dispatcher.” No
new Environments hop or Notification Runtime grant is needed for the recommended path.

### Sessions/Dispatcher: privileged native delivery

[`ProxyRunner`](../session_dispatcher/dispatcher/runtime_client.go) calls `CreateSession` and the four
command RPCs under the Sessions SVID after durable Dispatcher admission. Creation forwards the
recorded execution Identity; commands carry the retained command ID. The client replaces outgoing
metadata, so an original caller's credentials cannot leak into this component-only transport.
[Central placement routing](../session_dispatcher/dispatcher/central_delivery.go) calls the sampler's
own API instead; reuse of Runtime protobuf messages there is not a direct Runtime connection.

**Disposition:** retain these five methods as the narrow Sessions-to-Environments backend contract.
Public callers continue through SessionDispatcher, including admission, policy, queueing and
automatic wake/budget. Environments does not call Sessions. Preserve same-ID Runtime journal
reconciliation and uncertain outcomes; moving the transport must not add fresh-ID replay. No public
proxy Session-write role is needed.

### Central sampler: privileged execution, not Session commands

[The executor client](../central_sampler/internal/executor/executor.go) and
[operation methods](../central_sampler/internal/executor/operations.go) use all ten Executor RPCs
listed above under the sampler SVID. `OpenSession` owns the native adapter stream; subsequent calls
use the Session/destination/incarnation scope. This does not call Runtime `CreateSession` or bypass
Dispatcher admission for public Session commands. The sampler's own Session RPC implementation
imports Runtime request types but is a receiver, not another proxy client.

**Disposition:** retain the narrowly granted Executor API on Environments, independently of the
Sessions method grant. It is the intended remote execution boundary for a central sampler, not a
public Session-control API. Preserve authenticated destination/incarnation scope, native-frame
evidence, stream/handle recovery and unknown mutation outcomes; request IDs are not operation
receipts. Do not route tool execution through the durable command queue or add automatic replay.

### Controller: internal environment lifecycle

[Controller composition](../environments/cmd/main.go) uses
[`GatewayQuiescer`](../environments/internal/runtimecontrol/quiescer.go) for `Quiesce` and
[`GatewaySuspender`](../environments/internal/runtimecontrol/suspender.go) for `PrepareSuspend` and
`ResumeAdmission`, under its Controller SVID. Quiesce additionally sends the expected incarnation
UID and safely repeats its idempotent handshake. Suspend admission uses persisted generations and
an observed idle timestamp. These are leader-owned reconciliation calls, not standby RPC handling.

**Disposition:** after the proxy moves, make these calls internal to Environments' fenced Runtime
transport. No public EnvironmentService quiesce/admission endpoint is required. The Controller's observe-only
certificate still grants no direct Runtime mutations: internal forwarding must use the admitted proxy
peer, not broaden the observer's grant. Retain quiesce before ordinary teardown, idle expiry that only suspends, Firecracker checkpoint admission semantics,
incarnation fencing and existing idempotent retry behavior. Do not silently substitute disruptive
quiescence for the checkpoint-preserving suspend handshake.

### Direct Runtime channels and non-production clients

Only two production owners construct direct Runtime control connections:

- [The Controller watcher](../environments/internal/runtimecontrol/observer.go) sends
  `WatchState(observe_only=true)` to the accepted workload. It binds the incarnation and feeds the
  leader's in-memory facts for readiness, bootstrap, idle and quiesce decisions. Retain this private
  infrastructure channel; EnvironmentService RPC readiness never waits for leadership or a Runtime.
- [Each Gateway replica's observer](../session_dispatcher/internal/gateway/runner_observer.go) sends
  `WatchState` and holds the direct connection used by proxy/Executor forwarding. Move this owner
  into Environments with the proxy. Do not reintroduce Runtime-dials-Gateway, connection ownership
  Leases or epochs. Watch loss closes that replica's local route, not another replica's authority
  or proof that already admitted work stopped.

[The Python proxy client](../session_dispatcher/client.py) is used by fixtures/system tests, including
[the central-sampler driver](../system_tests/driver/central_sampler.py); it is not another deployed
service. System deletion/admission-reply-loss scenarios and Go acceptance fixtures also construct
Runtime peers, as do the [direct Runtime e2e driver](../env/e2e/testing/driver.py)
and [Firecracker host test probe](../firecracker/host/testing/runtime_probe/main.go#L121).
Keep genuine boundary tests and retarget their peers when the boundary moves; fixture credentials
must not become production grants. The CLI calls Dispatcher and EnvironmentService,
not these Runtime/Executor services; Actions likewise uses those owning APIs. The egress platform
routes do not expose a Runtime service prefix. Runtime-local native adapters and server code are
implementations of this boundary, not external proxy clients.

## Migration work and acceptance

The following is tracked work, not implementation release:

| Work                    | Required contract and acceptance                                                                                                                                                                                                                                                                                                                      |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Notification validation | Review admitted-versus-materialized behavior above; keep foreign/stale Identity and missing Session denial, source authorization, and delivery through Dispatcher. Test subscription while Runtime is offline and during materialization, plus later delivery after recovery. Remove its reader grant only after the changed caller image has rolled. |
| App raw diagnostics     | Direction approved by operator; add the narrow EnvironmentService method with exact Environment UID, explicit observation/incarnation/boot evidence and unavailable semantics; preserve operator diagnostic authorization and browser output. Retarget the App after the receiver is pinned.                                                          |
| App archive safety      | Resolve `SESSION_LIVE_STATUS_OWNERSHIP_DESIGN`, then replace this guard with the reviewed Session-status contract. Test shared-host siblings, busy/idle/unknown/stale evidence and offline/replaced environments; keep retained Sessions with no live attachment readable. Remove the App proxy grant only after both active paths move.              |
| Privileged transport    | The proxy/Executor and Controller-internal transport extraction landed (#16723 to #16775). Keep the Sessions and sampler method sets separate; reject other service SVIDs, workload Identities and delegated envelopes. Preserve UID/incarnation/boot fencing and current uncertain-outcome rules.                                                    |

Use receivers before senders, image before settings, then retire grants and old receiver code only
once callers have rolled and supported rollback no longer needs them. The physical proxy move may
retain App/Notifications temporarily; it must not treat those transitional grants as the final
boundary or wait on unrelated Session storage decisions. For each implementation PR, name its
boundary/system tests, update traffic diagrams and give a concrete configuration/image rollback.
Restore and pin an old receiver before rolling callers back to it. Never delete Sandbox storage,
Session records, identities or History to perform the cutover. This audit changes no API, manifest,
authentication mode, deployed name, proto wire name, data shape or staging resource.

## Review question

Should authorized Session admission/ownership suffice for subscription creation before owner
materialization, including while an attached Runtime is offline? The approved environment-independent
direction removes the Sandbox prerequisite; this question distinguishes admission from proof that
the Session owner materialized it.
