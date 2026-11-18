# Runtime proxy callers and the privileged boundary

Audit of devel `9f1fe4d2` (2026-11-15). **Disposition proposal for review; no traffic or
permission changes.** The operator released this audit after the EnvironmentService extraction.
The Runtime proxy and Executor serve from Environments.

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
[Executor protocol](../env/runtime/execution/executor.proto) are distinct services on Environments'
shared listener. [The method gate](../environments/runtimeproxy/server.go#L89) admits no
public `WatchState` method and rejects delegation on proxy and Executor calls. Its four service
roles are independent; their grants are in the
[proxy contract](../environments/runtimeproxy/SPEC.md).

No caller Identity is delegated through these methods. Component credentials admit
a trusted service, while destination headers/scope identify and fence the Sandbox; naming a Sandbox
does not authorize an arbitrary workload Identity. Runtime proxy and Executor receivers admit exact service SVIDs; Kubernetes bearers grant no
access. Keep the independent method/role grants as services separate
([authentication contract](../docs/workload_authentication.md#service-authorization-and-attribution)).
Deployed and wire names remain unchanged.

## Callers and dispositions

### App: diagnostics

The App's diagnostics read `EnvironmentService.GetRuntimeDiagnostics` and hold no raw-proxy grant.
Running marks and the archive guard use Dispatcher Session activity
([App contract](../app/README.md)); stale or unavailable activity does not imply an idle Session.

### Sessions/Dispatcher: privileged native delivery

[`RuntimeProxyClient`](../session_dispatcher/dispatcher/runtime_client.go) calls `CreateSession` and the four
command RPCs under the Sessions SVID after durable Dispatcher admission. Creation forwards the
recorded execution Identity; commands carry the retained command ID. The client replaces outgoing
metadata, so an original caller's credentials cannot leak into this component-only transport.
[Central placement routing](../session_dispatcher/dispatcher/central_delivery.go) calls the sampler's
own API instead; reuse of Runtime protobuf messages there is not a direct Runtime connection.

**Disposition:** retain these five methods as the narrow Sessions-to-Environments backend contract;
`HARNESS_HOST_EXTRACTION` moves them onto HarnessHost.
Public callers continue through SessionDispatcher, including admission, policy, queueing and
automatic wake/budget. Environments does not call Sessions. Preserve same-ID Runtime journal
reconciliation and uncertain outcomes; moving the transport must not add fresh-ID replay. No public
proxy Session-write role is needed.

### Central sampler: privileged execution, not Session commands

[The executor client](../sampler/internal/executor/executor.go) and
[operation methods](../sampler/internal/executor/transport/operations.go) use all ten Executor RPCs
listed above under the sampler SVID. `OpenSession` owns the native adapter stream; subsequent calls
use the Session/destination/incarnation scope. This does not call Runtime `CreateSession` or bypass
Dispatcher admission for public Session commands. The sampler's own Session RPC implementation
imports Runtime request types but is a receiver, not another proxy client.

**Disposition:** retain the narrowly granted Executor API on Environments, independently of the
Sessions method grant. It is the intended remote execution boundary for a central sampler, not a
public Session-control API. Preserve authenticated destination/incarnation scope, native-frame
evidence, stream/handle recovery and unknown mutation outcomes; request IDs are not operation
receipts. Do not route tool execution through the durable command queue or add automatic replay.

### Direct Runtime channels and non-production clients

Only two production owners construct direct Runtime control connections:

- [The Controller watcher](../environments/internal/runtimecontrol/observer.go) sends
  `WatchState(observe_only=true)` to the accepted workload. It binds the incarnation and feeds the
  leader's in-memory facts for readiness, bootstrap, idle and quiesce decisions. Retain this private
  infrastructure channel; EnvironmentService RPC readiness never waits for leadership or a Runtime.
- [Each Environments replica's observer](../environments/runtimeproxy/runtime_observer.go) sends
  `WatchState` and holds the direct connection used by proxy/Executor forwarding. Do not reintroduce
  Runtime-dials-Gateway, connection ownership Leases or epochs. Watch loss closes that replica's local route, not another replica's authority
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

| Work                    | Required contract and acceptance                                                                                                                                                                                                                                                                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Notification validation | Review admitted-versus-materialized behavior above; keep foreign/stale Identity and missing Session denial, source authorization, and delivery through Dispatcher. Test subscription while Runtime is offline and during materialization, plus later delivery after recovery. Remove its reader grant only after the changed caller image has rolled.              |
| App archive safety      | `DISPATCHER_RUNNING_STATUS` step 4 replaces this guard with the Dispatcher's running status. Test shared-host siblings, busy/idle/unknown/stale evidence and offline/replaced environments; keep retained Sessions with no live attachment readable. Both active paths already use EnvironmentService; this Session-status follow-up needs no App raw-proxy grant. |

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
