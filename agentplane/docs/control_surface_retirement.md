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

## Database artifact cleanup

The database-drop change removed the Gateway database resource, URL wiring, and migration init
container from current manifests. This cleanup removes the unused serving setting, migration sources
and test fixture, and the old migration-image publication path. It retains the shared CNPG PostgreSQL
Cluster and every other service database.

The HTTP/client paths likewise have distinct effects: app `POST /sandboxes/{name}/resume`
changes a Sandbox desired state, whereas `POST /threads/{thread_id}/resume` explicitly resumes
its native harness. The latter UI action stays when automatic resume-on-send is off; the suspended
indicator stays in both modes. The typed `POST /threads/{thread_id}/{submit-input,change-model,change-reasoning-effort,stop-runner-session,interrupt-turn}`
routes call the matching Dispatcher RPC, while
`GET /threads/{thread_id}/dispatches/stream` (`client.ts::dispatchesStreamUrl`) streams
their status; `GET /sandboxes/{name}/sessions/{session_id}/opening`
(`client.ts::getSessionOpening`) polls a separate opening. None is a duplicate command sender.
The browser's old Sandbox session GET is already removed; retained Threads come from History.

## Source links

- [RunnerControl and the reverse stream](runner_control.md) and [RunnerControl proto](../proto/runner_control.proto)
- [Runner RPCs](../proto/runner.proto)
- [Session Dispatcher API](../proto/session_dispatcher.proto)
- [Gateway proxy implementation](../runner_gateway/gateway/proxy.go)
- [Gateway runtime](../runner_gateway/README.md)
