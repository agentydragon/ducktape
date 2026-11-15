# EnvironmentService on every Environments replica

Approved serving contract (operator, 2026-11-15); implementation in progress. The
[agreed binary shape](https://agentydragon.github.io/ducktape/agentplane-organization/#binaries)
puts EnvironmentService beside the Sandbox Controller in `agentplane/environments/`.
Source baseline: devel `ae73f98d0fd17a12edd6ee62d77b6a4cbfb4bd07` (2026-11-14).

## Process and readiness

Today [the Controller entry point](../environments/cmd/main.go) starts a leader-elected
controller-runtime manager, metrics on `8080`, health probes on `8081`, and no RPC listener.
Its reconcilers and [Runtime observation](../environments/internal/controller/runtime_states.go)
remain leader-only. Each replica will also run an EnvironmentService gRPC listener on `8082`,
as a manager runnable with `NeedLeaderElection() == false`. It must not wait on `mgr.Elected()`.

Validate settings and certificates before starting the manager. The runnable owns binding,
serving, and closing the listener; bind/start/serve failure returns an error and stops the whole
process. Run the RPC policy watches on every replica under the same process context, independently
of the leader's Runtime watches. On termination, mark RPC health not serving and readiness false,
cancel watch streams, drain RPCs with a bounded graceful stop, then force-stop and join the workers
within the existing 30-second Pod termination grace. Use the current
[Gateway lifecycle](../runner_gateway/main.go) and [gRPC shutdown helper](../grpc/graceful/stop.go).
Leadership loss follows the manager's process shutdown; clients reconnect to another ready replica.

Replace the Controller's ready `Ping` with a check of the local RPC listener and its required policy
watch health. Keep liveness independent of remote dependencies. Readiness and gRPC health never
require leadership, a reconciler's first pass, a connected Runtime, or every Sandbox being Ready.
A standby becomes ready after its own serving dependencies initialize. Use the existing
[policy watch synchronization and health](../policy/reader/watches.go); stale policy continues to
fail authorization closed. No RPC uses the leader's in-memory Runtime facts as its inventory.

## Authority and concurrency

Every replica serves every EnvironmentService RPC, including mutations. Preserve
[the service's uncached watch client](../runner_gateway/environments/service.go),
[management authorization](../runner_gateway/environments/management.go), and
[explicit resume's guarded patch](../runner_gateway/environments/lifecycle/manager.go): exact
Sandbox UID, live caller and execution Identity, resume-token/wake-generation precondition,
resource-version conflict checks with reauthorization, and an uncertain outcome without automatic
replay. `REQUESTED` means desired state was accepted; the leader subsequently reconciles it.
Readiness, quiesce before suspend, and incarnation fencing remain Controller responsibilities.

Move explicit resume without its current constructor's mandatory `WakeBudget`: it reserves no budget
already ([lifecycle contract](../runner_gateway/environments/lifecycle/SPEC.md)). Split the automatic
wake implementation into Sessions, which keeps its own policy reader, guarded writes and PostgreSQL
budget. Environments acquires no database and makes no call to Sessions. Sessions keeps checking the
live Identity at admission ([Dispatcher](../runner_gateway/dispatcher/api.go)). Shared
[`environments/access`](../environments/access/policy.go) lives outside `internal/`, so Sessions
imports its `bind_session` rules without importing the serving package.
The two processes retain independent policy views and their existing fail-closed checks.

Recreate only EnvironmentService's slice of
[the Gateway auth composition](../runner_gateway/main.go): Kubernetes-backed workload SVID
resolution, live Identity lookup, `Methods`, exact service reader/manager/launcher/operator roles,
and configured typed delegation. Keep handler authorization, actor/effective-Identity separation,
method names and stream revocation checks unchanged. Follow
[auth stage 4](service_svid_authentication.md#sequencing-and-boundaries): use each caller's service
SVID only after the receiver image admits it; never fall back to a bearer. This extraction neither
broadens roles nor performs auth stage 5 cleanup.

## Service, certificates and callers

Extend the existing `sandbox-controller` Service with a named `grpc` port `8082`, selecting all
Controller replicas. Keep its metrics port and the deployed Deployment, ServiceAccount and image
names ([workload](../../cluster/cdk8s/agentplane/sandbox_controller.py)). Use the existing
`spiffe://agentplane/sandbox-controller` leaf for the server and pin that exact URI in clients;
[its certificate helper](../../cluster/cdk8s/agentplane/workload_tls.py) already requests both client
and server usage. Extend [the peer TLS listener URI validation](../net/peertls/credentials.go) to
accept this existing service identity as a server identity. Retain the workload CA, reload/expiry
behavior, and current Gateway server identity `spiffe://agentplane/session-dispatcher` on the old
endpoint. Do not introduce `spiffe://agentplane/environments` or rename deployed objects before
`RUNNER_GATEWAY_RENAME`.

Inventory and separate connections by API before changing targets. The Sandbox CLI is also a live
EnvironmentService caller: [`cli/environments.go`](../cli/environments.go) currently shares
`AGENTPLANE_SESSIONS_URL`. It needs
a separately accepted EnvironmentService URL and a method-selected local SVID route before the old
Gateway registration can retire; existing CLI processes and Sandbox launch configuration must be
accounted for during that cutover. Keep the existing endpoint until those callers have moved.

Service caller routing:

| Caller        | Routing in this slice                                                                                                                                                                                                                                                                                                                                           |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App           | Retarget only its EnvironmentService connection/facade to `sandbox-controller.{namespace}.svc:8082`, with the Controller server URI and the App's existing SVID roles. Leave Dispatcher and Runtime-proxy connections alone ([composition](../app/internal/server/runtime.go)).                                                                                 |
| Actions       | Split the current shared direct/delegated Dispatcher connections so EnvironmentService uses the new Service and server URI; preserve exact Action actor methods and effective Identity grants ([composition](../action_service/cmd/main.go)). Retire or adapt the `orchestration_v1` fixture's direct implementation import before making the service internal. |
| Notifications | There is no EnvironmentService client in current [composition](../notification_service/main.go): it calls Dispatcher and the Runtime proxy. Those targets stay until the separately released Runtime-proxy move; add no EnvironmentService grant or new hop here.                                                                                               |
| Sessions      | Automatic wake and admission stay local, with the public `environments/access` import. [Runtime calls](../runner_gateway/dispatcher/runner_client.go) keep their existing proxy endpoint until the Runtime-proxy move. No new EnvironmentService RPC replaces the local automatic-wake path.                                                                    |

## Implementation rollout and rollback

1. Land receiver/composition code and caller code accepting separate environment
   targets, with the new settings optional or disabled. The CLI accepts a separate EnvironmentService
   URL while preserving the current Sessions URL when it is absent. Keep manifests on existing settings and
   endpoints. During overlap both binaries import the existing public service implementation; move it
   into `environments/internal/service` only when the old Gateway registration retires. Move neither
   Dispatcher storage nor Runtime proxy/Executor in this slice. Pin those images before any ConfigMap
   sets the new keys: both the
   [Controller decoder](../environments/cmd/config.go) and its
   [authoring schema](../environments/settings.py) reject unknown fields today.
2. Enable the Controller listener, required API/policy RBAC and the narrowly scoped Service/network
   edges. Verify every replica, including a standby, serves authorized calls and refuses wrong
   peers/methods. Check the mounted server certificate, healthy policy watches and readiness on
   both Pods. Keep the old Gateway EnvironmentService registered.
3. Stage the Actions service-SVID launcher grant on the receivers wherever its existing namespaced
   launcher grant permits orchestration. Roll and verify those receiver grants before retargeting
   Actions; delegated read/resume grants confer no direct launch or management authority.
   With the receiver image pinned and running, and auth stage 4's exact grants present, switch each
   real EnvironmentService caller's target and verified server URI, including Sandbox CLI routes and
   launch-time URLs after their accepting tools image is pinned. Pin caller code before enabling
   new caller settings; roll callers and verify their effective configuration. Long-lived watches
   reconnect and reauthorize normally. Update system overview diagrams in the implementation PRs.
4. Retire Gateway component EnvironmentService roles, delegation and catalog from manifests first,
   retaining its decoder and registration. App (#16565) and Actions (#16573) have rolled on the
   Controller target. CLI routes and launch URLs (#16568) apply to new incarnations; the old live
   `codex-cleanup-tls` launch still lacks `AGENTPLANE_ENVIRONMENTS_URL` as of 2026-11-15 06:19 UTC.
   Coordinate its cycle before retiring the endpoint. Retain Dispatcher automatic-wake grants.
5. After the caller cutover and config rollout, remove the old registration and obsolete decoders;
   then remove unused RBAC only after that accepting image is pinned and running. Move the serving
   implementation internal once no external importer remains. Preserve automatic wake with Sessions.

Rollback switches caller configuration back to the still-serving Gateway first, keeping service
SVID authentication and exact method grants. After Flux applies it, check caller rollouts, list/get
and watch reconnection, explicit resume's generation/result, and continued leader reconciliation.
Then disable the new listener/config before reverting its accepting image. If the old registration
has already been removed, restore and pin that receiver before retargeting callers. Preserve all
Sandboxes, PVCs, identities and Session history.

Implementation acceptance adds deterministic two-replica coverage: standby readiness/RPCs without
leadership, concurrent resume from both replicas producing one guarded generation change, stale
UID/token and unavailable-policy denial, takeover and bounded drain. Preserve existing service SVID,
delegation, watch, explicit/automatic wake and Controller tests. Run the affected k3s Pod/Dispatcher
suites and name their exact targets in each implementation PR. Service/RBAC/Cilium changes also need
staging verification because those deployment objects have no system test. This note changes no
code, manifests, wire names or deployed behavior.
