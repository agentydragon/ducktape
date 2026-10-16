# Runner Gateway control-surface retirement

The Runner Gateway is the reverse RunnerControl listener and the per-Sandbox proxy for the runner's
Runner service. The app opens Sessions and sends durable commands through the Session Dispatcher;
it calls Runner.ListSessions and Runner.ResumeSession through the identity-fenced per-Sandbox proxy.
The Sandbox Controller calls Runner.Quiesce through that proxy. The runner initiates RunnerControl.Connect
through its egress route, and the Gateway routes tunneled Runner RPCs to the replica holding the stream.

The Gateway has no Session RPC service, its proto/client, or partial Session registry runtime.
#10716 removed the unused `SubmitCommand` wrapper; Dispatcher command admission and Runner
per-operation RPCs remain. The Gateway serves standard gRPC health, the reverse control stream, the
Runner proxy, live Runner inventory, explicit app resume, and controller quiesce. The app's
`runner_gateway_target` remains for the gRPC health probe; its control target, token and timeout remain
for live Runner calls.

## Session and command ownership

| Operation              | Current owner and path                                                                                                                                                                                                       |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Open a Session         | App → Session Dispatcher `OpenSession` → Runner `OpenSession` through the Gateway proxy. Identical retries return the existing attachment; incompatible reuse remains a Runner conflict.                                     |
| Admit commands         | App and Notifications → Session Dispatcher. The Dispatcher durably records admission and invokes per-operation Runner RPCs through the proxy.                                                                                |
| Read live inventory    | App and Notifications → `Runner.ListSessions` through the per-Sandbox control Service. The proxy checks the current Sandbox name, UID and owner. History summaries are retained views, not a substitute for live membership. |
| Explicit native resume | App checks retained Thread/spec state, then calls `Runner.ResumeSession` through the same identity-fenced proxy. This does not wake a suspended Sandbox.                                                                     |
| Quiesce                | Sandbox Controller → `Runner.Quiesce` through the proxy before stopping or deleting a runner.                                                                                                                                |
| Runner connection      | Runner → outbound `RunnerControl.Connect` stream through the egress route. The holder publishes connection Leases and EndpointSlices; tunneled Runner responses return on that stream.                                       |

The Gateway-specific `CreateSession`, `ResumeSession`, `ListSessions` and
`BatchGetSessionSandboxes` wrappers are removed. The Runner methods remain because they have current
callers. Command admission and delivery use Session Dispatcher and the per-operation Runner RPCs;
the journal's `CommandAdmitted` history still stores the shared `Command` message. History's Session
Event archive and the Dispatcher queue are independent state owners.

## Retained database migration gate

The Gateway serving process no longer opens or reads the `sandbox_commands` database. The legacy
`session` table remains in the schema and its migration init container continues to run while the
registry-free image rolls out. The optional serving setting accepts an old Pod configuration value
but is not used by the server; the migration command has its own database URL setting.

The separate app `Runner.ResumeSession` caller rollout was verified on 2026-10-16: testing app and
Runner Gateway are 1/1 Ready, staging app and Runner Gateway are 2/2 Ready, and every Pod runs image
`devel-*4693fc9`; the image includes #10709's app change. This clears the direct-resume caller gate.
The registry-free receiver image has not rolled out yet, so the database-drop gate remains closed.

After the registry-free image is Ready in testing and staging and deployed callers no longer require
the old receiver, a separate PR removes the `session` table and Gateway-owned database resources.
That PR removes the CNPG Database, role, generated Secret, migration init container, URL wiring and
migration/image publication path. It retains the shared CNPG PostgreSQL Cluster and every other
service database. No live database is mutated by this runtime-retirement change.

## Source links

- [RunnerControl and the reverse stream](runner_control.md) and [RunnerControl proto](../proto/runner_control.proto)
- [Runner RPCs](../proto/runner.proto)
- [Session Dispatcher API](../proto/session_dispatcher.proto)
- [Gateway proxy implementation](../runner_gateway/runner_proxy.py)
- [Gateway runtime and retained migration code](../runner_gateway/README.md)
- [Rollout and database-drop gates](../plans/task_dag.md#12-the-runner-gateway-serves-the-runners-own-service)
