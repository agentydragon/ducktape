# Runner control stream

Status: **accepted design, deployed.** The Runner Gateway's
control listener (`runner_gateway/runner_control.py`), reachable through the egress proxy,
authenticates, fences and keeps connections alive, and carries command deliveries and lifecycle
requests (`runner_gateway/lifecycle_control.py`) for Sandboxes routed to it. The runner dials it when started
with `--control-target` (`runner/control.py`), which the Sandbox Service passes to every Sandbox's
runner (`runner_control`); runner Pods admit no connections. This is the contract it implements. It
covers command delivery, authentication, connection ownership, lifecycle
requests (listing, opening, resuming and sealing Sessions), and the push of Events to the runner's
ingress.

## Shape

The runner dials out to two endpoints its Sandbox names. It pushes its journals' Events to one
**ingress** ([Event ingress](#event-ingress)), and holds a control stream to the Runner Gateway for
commands, receipts and lifecycle requests. Each runner process is a gRPC client of the Runner
Gateway and holds one HTTP/2 connection to it. On that connection it keeps one long-lived
bidirectional `Connect` stream, which carries command delivery and lifecycle requests for every
Session in its state directory. Events never travel on it, so no event catch-up can starve a
command. The runner is the client, so service-to-runner traffic is messages on streams the runner
opened, never reverse RPCs. No operation creates a Session or starts a stopped harness.

```protobuf
service RunnerControl {
  rpc Connect(stream RunnerFrame) returns (stream ServiceFrame);
}

message RunnerFrame {
  oneof frame {
    Hello hello = 1;                  // first frame, exactly once
    CommandReceipt receipt = 2;
    Heartbeat heartbeat = 3;
  }
}

message ServiceFrame {
  oneof frame {
    Welcome welcome = 1;              // reply to Hello, exactly once
    DeliverCommand deliver = 2;
    Heartbeat heartbeat = 3;
  }
}

message Hello {
  string runner_boot_id = 1;          // random per runner process start
}

message Welcome {
  uint64 connection_epoch = 1;
}

// The runner deduplicates on `command.command_id` within a session, so redelivery is safe.
message DeliverCommand {
  string session_id = 1;
  agentplane.proto.command.Command command = 2;
}

message CommandReceipt {
  string session_id = 1;
  string command_id = 2;
  oneof outcome {
    // The runner journal's CommandAdmitted entry, with its cursor. Fresh or already journaled.
    agentplane.proto.event_log.EventEntry admitted = 3;
    CommandRefused refused = 4;
  }
}

message CommandRefused {
  enum Reason {
    REASON_UNSPECIFIED = 0;
    REASON_SESSION_UNKNOWN = 1;
    REASON_HARNESS_STOPPED = 2;
    REASON_COMMAND_CONFLICT = 3;      // same command_id, different payload
    REASON_INVALID_COMMAND = 4;
  }
  Reason reason = 1;
  string detail = 2;
}

message Heartbeat {}
```

- **Versioning.** The proto is `agentplane/proto/runner_control.proto`, package
  `agentplane.proto.runner_control`. An incompatible change, a rename included, rides a clean
  cutover: the operator retires every Sandbox on the old protocol, and no dual path is served.
  Nothing is negotiated: every connection carries commands and lifecycle requests. A frame of a
  kind the receiver does not know fails the stream, and an RPC the service does not implement
  returns `UNIMPLEMENTED`, so a skew shows up at first use rather than as silently dropped work; a
  new frame kind lands in its receiver before any sender uses it.
- **Later streams bind to the epoch.** Every RPC other than `Connect` carries the connection epoch
  from `Welcome` in metadata and is refused once that epoch is superseded.
- **Message size.** gRPC's default 4 MiB per message.
- **Receipts are facts.** A receipt reports the runner journal. Re-delivering a command already
  journaled with the same payload returns its existing `CommandAdmitted` entry, which the runner
  already guarantees by deduplicating on `command_id`. The service never infers admission from a
  stream write, a heartbeat or HTTP/2 flow-control progress. Which refusal reasons are terminal for
  a submission is the [command admission](../runner_gateway/API.md#sessions-and-commands)
  contract's decision, not the control stream's; today none is, and a refusal leaves it pending.
- **Status codes.** The service ends a stream with a gRPC status naming the cause:
  `UNAUTHENTICATED`, `PERMISSION_DENIED` (not a Sandbox Pod of this service), `ABORTED` (superseded by a
  newer connection), `INVALID_ARGUMENT` (protocol error) or `UNAVAILABLE` (replica draining or the
  connection's maximum age). The runner logs the status and reconnects, except that
  `PERMISSION_DENIED` backs off to its maximum interval.

## Event ingress

Status: **accepted design (operator, 2026-10-10 PDT); deployed.** The runner pushes
when started with `--ingress-target` (`runner/ingress.py`), which the Sandbox Service passes to new
runners from its `runner_ingress` setting, and reports `flushed` from what it recorded. The History
Service serves the ingress (`history_service/ingress.py`) on a listener of its own, and follows no
runner.

The runner pushes each Session's journal to one **ingress**, the endpoint its Sandbox names, and
nothing else reads its journals. The History Service is the ingress when history is kept. A client
that keeps no history implements the ingress itself and sends commands and lifecycle calls through
the Runner Gateway like any other client. Fan-out to further readers is the ingress's concern, or
a service's behind it, never a second destination on the runner.

Rejected: following runners through the Sandbox Service, which made it fan out a journal it does
not keep and needed retention holds for durability; and merging the raw log into the Sandbox
Service, which makes a historyless client unlike any other and couples live delivery to the history
database.

```protobuf
// agentplane/proto/runner_ingress.proto
service RunnerIngress {
  rpc Push(stream PushFrame) returns (stream Acknowledgement);
}

message PushFrame {
  oneof frame {
    agentplane.proto.runner.Attached announce = 1;  // first frame, exactly once
    agentplane.proto.event_log.EventEntry entry = 2; // the journal's, verbatim, in cursor order
    agentplane.proto.runner.Sealed sealed = 3;      // terminal
    Heartbeat heartbeat = 4;
  }
}

message Acknowledgement {
  uint64 cursor = 1;                  // every entry through it is committed; never decreases
  bool sealed = 2;                    // the seal at `cursor` is committed
}
```

The ingress has its own package, `agentplane.proto.runner_ingress`: another server implements
it, under its own token audience, and a historyless client needs only it.

1. **One call per Session.** The runner opens a `Push` call for every Session in its state
   directory whose seal the ingress has not acknowledged: at startup, when `OpenSession` creates a
   Session, and after a call is lost. Its calls share one HTTP/2 connection to the ingress, apart
   from its `Connect` connection, and each is its own HTTP/2 stream, so one Session's catch-up
   stalls neither another Session's live entries nor any command.
2. **Announcement.** The first frame, `announce`, is the Session's `Attached` snapshot: its Session
   ID, the UUID the client minted and passed in `OpenSession`, with spec, harness and setup state
   and the journal's last cursor. Every call announces again; the first the ingress accepts adds
   the Session to its index of Sessions. The ingress answers with an `Acknowledgement` of what it
   has already committed for the Session, cursor zero for a new one.
3. **Entries.** The runner then sends the journal's `EventEntry`s after that acknowledgement's
   cursor, replayed and then live, reading the journal in pages as the stream drains. Once the
   journal is sealed and every entry through the seal is sent, it sends `sealed` and nothing more.
4. **Acknowledgement.** The ingress acknowledges a cursor only after committing every entry through
   it, and sets `sealed` only after committing the seal. The runner records the latest
   acknowledgement per Session on its state volume and resumes after it; it deletes no journal
   entry on acknowledgement. Delivery is therefore at least once and idempotent by cursor: an entry
   at or below the committed cursor that equals the committed one is accepted and ignored.
5. **Backpressure.** The runner sends at most 1024 entries per Session beyond the latest
   acknowledgement and waits for acknowledgements past that; gRPC's per-stream flow control bounds
   the bytes in flight. The ingress commits and acknowledges in batches of its choosing.
6. **Liveness.** On an idle call the runner sends `heartbeat` and the ingress repeats its latest
   acknowledgement every 10 s; either side ends a call after 30 s without a frame.
7. **Replicas.** Any ingress replica serves any Session's call, and its store serializes appends
   per Session, so two calls for one Session, such as a restarted runner's racing its
   predecessor's, are harmless.

Rejected: one `Push` stream per runner multiplexing its Sessions. It saves streams but puts every
Session behind one flow-control window, so a catch-up would delay every live Session and the
runner would have to schedule fairly itself; and its writer binding and acknowledgement would be
per frame instead of per call.

**Writer binding.** The ingress authenticates every call's bearer for its own audience with a
TokenReview, as the shared `WorkloadPrincipalAuthenticator` does
([workload authentication](workload_authentication.md)), and resolves no Sandbox. The first accepted announcement binds the Session to its writer's ServiceAccount, by namespace, name
and the UID TokenReview returns, in the transaction that records the Session, so of two concurrent
first writers one is refused. A call for the Session from any other ServiceAccount fails with
`PERMISSION_DENIED`. Every Sandbox runs as its own ServiceAccount, so this ties the Session to its
Sandbox without the ingress knowing Sandboxes. The Pod is not part of the binding, so a replacement
Pod of the same Sandbox, opening the same journal from the state volume, keeps writing; the UID
makes an account recreated under the same name a different writer. Within the Sandbox the trust
boundary is the control stream's: the binding keeps other Sandboxes out, not processes beside the runner.

**Status codes.** The ingress ends a call with `UNAUTHENTICATED`, `PERMISSION_DENIED` (another
writer's Session), `INVALID_ARGUMENT` (a Session ID that is not a UUID, a frame out of order, a
cursor gap, a frame after `sealed`), `FAILED_PRECONDITION` (an entry or source that differs from
what it committed at that cursor) or `UNAVAILABLE`. The runner cancels a call itself, and treats it as
`FAILED_PRECONDITION`, when the first acknowledgement is beyond its journal's last cursor, since the
ingress then holds entries this journal never wrote.

**Ingress unreachable or refusing.** The journal is the buffer. The runner keeps admitting commands
and journaling, and each Session's call retries with the control stream's backoff, 0.5 s to 30 s.
`PERMISSION_DENIED` and `FAILED_PRECONDITION` back off at the maximum and are logged as errors,
since only an operator resolves them; a refused Session holds up no other. Unacknowledged lag is
bounded only by the state volume, and a Sandbox cannot finish teardown while any is left.

**Endpoint and authentication.** The runner is started with its ingress and control targets, which
only the component creating the Sandbox's Kubernetes objects sets, from its own configuration: one
ingress for every Sandbox it creates (the History Service, or a historyless deployment's own
sink), and the control target is always the Runner Gateway. A create request cannot choose
either; an ingress per Sandbox is not built. The runner reaches the ingress through its egress sidecar with a placeholder bearer, like
the control listener ([Path through the egress proxy](#path-through-the-egress-proxy)). An egress policy
the Sandbox Service binds at creation, not a launch-selectable one, admits the ingress host and
substitutes the Pod's projected token for that ingress's own audience: `agentplane-runner-ingress`
for the History Service, and an audience of its own for a historyless client. No other recipient
can replay a token the ingress saw, and an ingress token opens no control connection.

**Sealing.** `Seal` on the control stream seals every journal and returns each Session's seal cursor
with `flushed`, which is true once the ingress has acknowledged that seal. The runner answers from
its recorded acknowledgements, so the report survives a runner restart. Sealing again returns the
same cursors and the current `flushed`, so teardown repeats `Seal` every 10 s until every Session
is flushed, and only then may the Sandbox's volume go. A runner that cannot report leaves the
Sandbox in sealing for the operator.

## Connection records in Kubernetes

Status: **built in the Runner Gateway.** These records hold connection ownership and liveness
(§ Connection ownership and fencing); the gateway keeps no other connection or command state.

Per Sandbox, all in the Sandbox's namespace and owned by the `Sandbox`, so deleting it removes them:

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: <sandbox>-runner
  annotations:
    agentplane.allegedly.works/pod-uid: <the Pod the connection authenticated as>
    agentplane.allegedly.works/runner-boot-id: <from Hello>
spec:
  holderIdentity: <replica Pod name>
  leaseTransitions: <connection epoch>
  renewTime: <last frame received, written at most every 15 s>
  leaseDurationSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: <sandbox>-control
spec:
  clusterIP: None # headless: DNS returns the endpoint, no ClusterIP or kube-proxy rules
  ports: [{ name: grpc, port: 8080 }]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: <sandbox>-control
  labels: { kubernetes.io/service-name: <sandbox>-control }
  annotations: { agentplane.allegedly.works/connection-epoch: "<epoch>" }
addressType: IPv4
endpoints: [{ addresses: [<owning replica's Pod IP>] }]
ports: [{ name: grpc, port: 8080 }]
```

No routing record is kept: runners dial out, so nothing is routed inbound. Agent-sandbox already names a Service after the
Sandbox, hence `-control`.

- **Accepting a connection.** After authenticating `Hello`, the replica updates the Lease with the
  `resourceVersion` it read: holder itself, transitions plus one, fresh renew time and annotations.
  A conflict means another replica won a race; it re-reads and retries, and the runner's own
  newest-connection rule decides which stream survives. It then writes the EndpointSlice with the new epoch, and only then sends `Welcome`.
  An EndpointSlice write carrying an older epoch than the one stored is skipped.
- **Liveness.** The holder renews the Lease with each liveness write. A renew that finds another
  holder or a newer epoch means the connection was superseded, and the replica ends the stream with
  `ABORTED`.
- **Reading.** Every replica keeps an informer on Leases in the namespace: a replica checks that no
  newer connection superseded the one it holds before sending on it. No read goes to the API server per request.
- **Stale endpoints.** During a roll, DNS can return a replica that no longer holds the stream for a
  few seconds; it refuses, and the client retries. Nothing reads the endpoint as proof of
  ownership.
- **Costs.** One Lease renewal per connected Sandbox every 15 s and one EndpointSlice write per
  reconnect. The Runner Gateway has RBAC to write Leases, Services and EndpointSlices in the
  Sandbox namespace.

## Lifecycle operations

Status: **built.** The Runner Gateway sends every Session call and teardown's seal over the control
stream (`runner_gateway/lifecycle_control.py`). A runner refuses `OpenSession` for a Session it created but could not
start with `FAILED`; asked again, it confirms the stopped Session, so `CreateSession` follows an
`OpenSession` that confirms a stopped harness with `ResumeSession`. The App and the Notification
Service reach the holder through the control Service (`control_target`, a required setting).

Each control request names an explicit verb; nothing is inferred from which fields are present.

```protobuf
message ServiceFrame {
  oneof frame {
    // ...
    ControlRequest control = 6;
  }
}

message RunnerFrame {
  oneof frame {
    // ...
    ControlResult control_result = 4;
  }
}

message ControlRequest {
  string request_id = 1;              // chosen by the service; the runner answers each once
  oneof verb {
    ListSessions list_sessions = 2;   // read
    Initialize initialize = 3;        // bootstrap once per Sandbox; an identical retry replays
    OpenSession open = 4;             // create with a spec and start it; an existing Session is
                                      // confirmed only, never restarted
    ResumeSession resume = 5;         // restart a stopped harness on its retained spec
    Seal seal = 6;                    // fence, stop every harness and setup, seal every journal
  }
}

message ControlResult {
  string request_id = 1;
  oneof outcome { /* one result per verb */ ControlRefused refused = 9; }
}
```

The contract's rule becomes: no operation creates a Session or starts a harness implicitly; only
`OpenSession` and `ResumeSession` do. `Seal` travels on `Connect` behind every command already
sent, so no command delivered before it lands after it. Only the replica holding the runner's
stream sends control requests: callers reach it through the Sandbox's control Service
([Connection records](#connection-records-in-kubernetes)), and background work such as sealing runs on the replica holding the Lease. Requests are bounded by the
calling RPC's deadline, like command deliveries, and a reconnect replays none of them.

| Runner Gateway caller | Control operation                                     |
| --------------------- | ----------------------------------------------------- |
| `ListSessions` RPC    | `ListSessions`                                        |
| `CreateSession`       | `Initialize`, then `OpenSession` with the client's ID |
| `ResumeSession`       | `ResumeSession`                                       |
| `Teardown` sealing    | `Seal`, repeated until every Session is flushed       |
| `SubmitCommand`       | `DeliverCommand`                                      |

The VM experiment's runtime acceptance drives the guest runner directly through a port-forward and
moves with the selected VM control path.

## Prior art and alternatives

Two harnesses already ship a dial-out control channel, and the choices above borrow from both:

- **Claude Code RemoteIO** (`--sdk-url`, [our interoperability spike](../harness_tests/x/claude_remote_io/README.md)):
  the worker registers an epoch the server fences, posts heartbeats, receives commands over an
  SSE stream with sequence numbers and `Last-Event-ID` replay, and uploads output with HTTP
  `POST`s acknowledged after commit. The connection epoch here is its worker epoch.
- **Codex remote control** (app-server dialing `wss://…/remote/control/server`): it enrolls once
  for a durable server ID, sends a protocol version header, multiplexes client streams in
  envelopes carrying `seq_id`, keeps sent-but-unacknowledged envelopes in a bounded buffer that it
  resends with a subscribe cursor on reconnect, and splits large messages into segments. A second
  connection for an enrollment that is already online is refused with `409`, and stale ownership
  is a reported failure with no takeover path (openai/codex#52099). That is why a new connection
  here supersedes the old one rather than being refused.

Alternatives for the transport itself:

1. **A gRPC bidirectional stream** (this design). It is the RPC stack every other Agentplane hop
   uses, with typed stubs, deadlines, status codes and per-stream flow control. The egress proxy
   already substitutes credentials in gRPC metadata on a bidirectional stream (BuildBuddy's Build
   Event Service, [egress spec](../egress/SPEC.md)).
2. **One WebSocket of protobuf frames.** This was the operator's first selection, and the egress
   proxy relays WebSockets too. It needs hand-rolled close codes, versioning and a multiplexing
   scheme, and one byte stream gives no per-operation flow control. gRPC was chosen when the
   design was reviewed.
3. **RemoteIO's shape: SSE down, HTTP `POST` up.** Every upload re-authenticates, which makes
   revocation immediate, but it costs a request per upload batch and pairs a stream with separate
   requests that can land on different replicas, so receipts would need the same cross-replica
   routing as commands. Not chosen.
4. **Codex's envelope wholesale, with a transport-level acknowledgement buffer.** Its unacked
   buffer exists because the relay is not durable. Here the runner journal and the service
   database already are, so command delivery relies on idempotent re-delivery instead of a second
   ledger. Event ingress takes its cursors and acknowledgements from this model
   ([Event ingress](#event-ingress)).

## Authentication and incarnation binding

The runner dials the Runner Gateway's control listener through the Pod's egress sidecar, like
every other destination it reaches; the Sandbox's network fence admits nothing else. It sends a
placeholder in `authorization: Bearer` metadata on every RPC. The egress proxy authenticates the Pod
hop and substitutes that Pod's own projected token for the dedicated audience
`agentplane-runner-control`, the same mechanism and per-destination audience that
[notifications](../notification_service/README.md) uses. The token is projected only into the
sidecar, so neither the runner nor the harness ever holds it.

The audience is distinct from the Sandbox Service's and Runner Gateway's (`agentplane-sandbox-service`,
`agentplane-runner-gateway`) and from
the shared `agentplane-egress` audience that the LLM ingress and Action Service receive, so none of
those recipients can replay a token they saw as a runner connection, and a control token cannot
call the Sandbox Service or Runner Gateway API.

The rule that admits the control host lives in its own egress policy, which the Sandbox Service
binds to every Sandbox at creation. It is not one of the launch-selectable
policies, so choosing a narrow egress set cannot cut a Sandbox off from its own control plane.

When a `Connect` stream opens, the Runner Gateway authenticates the bearer with the shared
`WorkloadPrincipalAuthenticator` ([workload authentication](workload_authentication.md)) and then
resolves the principal the way destination resolution does, in reverse:

1. The Pod named by the token exists with the token's UID and is not being deleted.
2. It has exactly one controller reference, a `Sandbox` of the agent-sandbox API, and that Sandbox
   is in the service's inventory with the same UID.
3. The Sandbox runs as the token's ServiceAccount.

A VM environment's runner reaches the control listener from its `virt-launcher` Pod's egress relay, and that
Pod resolves to its environment through its own controller chain under the same rules.

Names, IPs, claimed Session IDs and anything in `Hello` are not identity. The connection is bound
to the Sandbox UID and Pod UID that resolution returned. A Sandbox being deleted is still
accepted, because the runner must stay reachable while it seals its Sessions; a deleted Pod fails
TokenReview.

**Revocation.** Every RPC is authenticated when it opens. With each liveness write (every 15 s) the
owning replica resolves the connection's Pod and Sandbox again, and ends the connection's streams
when its Pod is deleted or replaced or its Sandbox's ServiceAccount changes. As a backstop for a
missed watch event, it ends every `Connect` stream after one hour; the runner reconnects and
authenticates again.

**Trust boundary.** The token proves the Pod, not the runner process. The harness and every
process it launches share the runner's container and state volume, so a process inside the Sandbox
can open a competing control connection. That gains nothing it does not already have: it can
already rewrite the runner's journal on the shared volume or kill the runner. The control stream
therefore authenticates the Sandbox incarnation and claims no process isolation. Distinguishing
the runner from agent processes needs a resource boundary inside the Sandbox, such as a VM guest
that keeps the runner outside the agent's reach.

## Connection ownership and fencing

The replica holding a Sandbox's connection records it in Kubernetes, in the Sandbox's namespace and
owned by the `Sandbox`, so deleting it removes them:

- a `coordination.k8s.io/v1` Lease `<sandbox>-runner`: `holderIdentity` is the replica's Pod name,
  `leaseTransitions` the connection epoch (one more for every accepted connection), `renewTime` the
  last frame received (written at most every 15 s), `leaseDurationSeconds` 30, and annotations
  `agentplane.allegedly.works/pod-uid` and `agentplane.allegedly.works/runner-boot-id` the Pod the
  connection authenticated as and the boot ID from `Hello`;
- a headless, selector-less Service `<sandbox>-control` on the Runner Gateway's gRPC port, and its
  EndpointSlice `<sandbox>-control`, naming the holder's Pod IP and annotated
  `agentplane.allegedly.works/connection-epoch`.

Accepting a connection updates the Lease with the `resourceVersion` it read (creating it if absent),
re-reading and retrying on a conflict; then writes the EndpointSlice unless it already names a newer
epoch; then sends `Welcome` with the new epoch. The previous owner, if it still holds a stream, finds
another holder or a newer epoch at its next renewal and ends the stream with `ABORTED`. A runner
keeps at most one `Connect` stream: it cancels the old one before opening a new one, and acts only
on frames from the connection that received its latest `Welcome`.

Every replica keeps a watch cache of these Leases. The holder's check that no newer connection
superseded its own comes from it, never from the API server per request. Nothing reads the
EndpointSlice as proof of ownership.

Stale sends are harmless rather than impossible. A replica that has not yet noticed it was
superseded can write to a dead stream, and a command that reaches the runner twice is deduplicated
by `command_id`. The epoch decides which replica sends new deliveries and which connection's
heartbeats count; it is not a Session, Event or command identity, and a new epoch neither starts a
harness nor proves that anything failed.

## Command delivery

`SubmitCommand` is served only by the replica holding the Sandbox's runner connection; callers reach
it through the Sandbox's control Service, as they do for lifecycle requests, and any other replica
refuses with `UNAVAILABLE`. The holder checks that the Session runs in that Sandbox incarnation,
sends `DeliverCommand` with the runner's Session ID and the command, and waits within the RPC's
deadline for the `CommandReceipt` naming the same Session and command ID. Concurrent deliveries of
one command share its receipt, and a receipt nothing awaits is dropped.

The gateway records no command. A lost reply, a passed deadline or an ended connection leaves the
outcome to the runner's journal, and the caller retries with the same command: the runner answers a
command it already journaled with its original `CommandAdmitted` entry, even after its harness
stopped or its journal was sealed. A reconnect replays no delivery, so a stale interrupt or stop is
never fired at a reconnecting runner. Where a command stands is read from the Session's history:
its `CommandAdmitted` entry, which the History Service holds once the runner has pushed it.

## Heartbeat and liveness

Each side sends `Heartbeat` on `Connect` every 10 s, and ends the stream after 30 s without any
frame from the peer. A runner reconnects with exponential backoff and jitter, from 0.5 s up to 30 s, and
retries indefinitely. A live connection is not readiness to act: it says nothing about harness
state, an active turn or whether a command was carried out, and losing the connection proves none of
those either.

## Path through the egress proxy

A gRPC client behind an HTTP proxy opens a `CONNECT` tunnel, and the egress proxy intercepts the
tunnel to read and substitute metadata. It reads the plaintext HTTP/2 the control listener and the ingress
speak inside that tunnel, and decides a gRPC request from its headers, so the stream's messages pass
through as they are written rather than after the stream closes. Long streams also cross proxy
replica drains, which end them; the runner reconnects like after any other `UNAVAILABLE`.
