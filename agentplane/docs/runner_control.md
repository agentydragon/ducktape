# Runner control transports

Runner configuration must select `control.mode: forward` or `control.mode: reverse`.
There is no default or automatic fallback. The Gateway's required `runner_transports` allowlist
can admit either mode or both. A SandboxTemplate's required `spec.runnerTransport` selects the
mode for its next workload; the live Pod or VMI carries that selection, independently of later
template edits. History Event ingress and durable runner command admission have the same contract
in both modes.

## Forward transport

The runner serves native gRPC on one private listener: `Runner`, `RunnerExec` and standard gRPC
health. The Gateway derives its address from the current workload's Pod IP and the runner port;
KubeVirt forwards that port into the guest. Only Gateway workloads may reach it.

One Gateway replica claims the existing runner Lease, then calls `Runner.WatchState` with the
Sandbox UID, incarnation Pod UID and claimed epoch. The runner adopts that incarnation before
publishing its boot ID and facts, then sends snapshots on changes and periodically. The Gateway
publishes authenticated facts and the boot ID to the Lease and exposes the existing
`<sandbox>-control` proxy route. An ownership claim without an observed boot ID is not a connected
runner. Other replicas wait for the owner's Lease to expire before taking over.

Unary Runner calls and each `RunnerExec.Connect` stream carry the Sandbox UID, incarnation UID,
runner boot ID and epoch in metadata. The runner rejects calls outside its active observer epoch.
A superseded observer cannot clear its successor. Observation loss stops executor streams and
new control calls; admitted journal work remains runner-owned and continues independently.
Each executor connection uses an ordinary bidirectional gRPC stream without tunnel channel IDs.

`control` is a tagged object containing either `mode: forward`, `listen_address` and `tls`, or
`mode: reverse`, `target` and `tls`. TLS settings contain `cert_file`, `key_file` and `ca_file`.
Contradictory or partial settings fail startup. Gateway `runner_listen_address` is required exactly
when its transport allowlist includes `reverse`; its public caller listener remains separate.

## Reverse transport

In reverse mode, the runner dials two endpoints its Sandbox names. It pushes its journals' Events to one
**ingress** ([Event ingress](#event-ingress)), and holds a control stream to the Runner Gateway for
commands and lifecycle requests. Each runner process is a gRPC client of the Runner Gateway and
holds one HTTP/2 connection to it. On that connection it keeps one long-lived bidirectional
`Connect` stream. Events never travel on it, so no event catch-up can starve a command. No
control-stream reconnect creates a Session or starts a harness by itself.

The runner serves the `Runner` service, and the gateway calls it **through** `Connect`: each unary
call travels as a `Call` frame and comes back as a `Reply` with the call's gRPC status
([Tunnelled calls](#tunnelled-calls)). Both sides use the generated servicer and stub, so a call
keeps its deadline and status code as if the gateway had dialed the runner.

```protobuf
service RunnerControl {
  rpc Connect(stream RunnerFrame) returns (stream ServiceFrame);
}

service Runner {
  rpc WatchState(WatchStateRequest) returns (stream WatchStateResponse); // forward only
  rpc ListSessions(runner.ListSessionsRequest) returns (runner.ListSessionsResponse);
  rpc OpenSession(OpenSessionRequest) returns (OpenSessionResponse);
  rpc GetStatus(runner.GetStatusRequest) returns (runner.RunnerStatus);
  rpc Quiesce(runner.QuiesceRequest) returns (runner.QuiesceResponse);
  rpc SubmitInput(SubmitInputRequest) returns (SubmitInputResponse);
  rpc ChangeModel(ChangeModelRequest) returns (ChangeModelResponse);
  rpc ChangeReasoningEffort(ChangeReasoningEffortRequest) returns (ChangeReasoningEffortResponse);
  rpc InterruptTurn(InterruptTurnRequest) returns (InterruptTurnResponse);
}

message RunnerFrame {
  oneof frame {
    Hello hello = 1;                  // first frame, exactly once
    RunnerHeartbeat heartbeat = 3;
    grpc_tunnel.Reply reply = 5;      // to a `call` on this stream
  }
}

message ServiceFrame {
  oneof frame {
    Welcome welcome = 1;              // reply to Hello, exactly once
    Heartbeat heartbeat = 3;
    grpc_tunnel.Call call = 7;        // a `Runner` call
  }
}

message Hello {
  string runner_boot_id = 1;          // random per runner process start
}

message Welcome {
  uint64 connection_epoch = 1;
  string incarnation_uid = 3;         // the Pod UID the connection authenticated as
}

// Dispatcher uses one RPC per command. Each takes `session_id`, `command_id` and its own fields
// (`text`, `model`, `effort`, `turn_id`; none for a stop), returning the journal admission. A
// `command_id` is unique across the operations: the same ID under another operation or payload
// fails `ALREADY_EXISTS`.

message Heartbeat {
  reserved 1;
  reserved "facts";
}

message RunnerHeartbeat {
  optional RunnerFacts facts = 1;     // on a change, and first after every Welcome
}

// Each RunnerHeartbeat carrying it replaces the gateway's record of all the facts.
message RunnerFacts {
  QuiesceStage quiesce_stage = 1;     // unspecified: not quiescing
  google.protobuf.Timestamp idle_since = 2;  // absent while any listed blocker is present
  Bootstrap bootstrap = 3;            // running, succeeded, failed or interrupted
  bool setups_running = 4;
  repeated agentplane.proto.AutosuspendBlocker autosuspend_blockers = 5;
}
```

- **Versioning.** The protos are `agentplane/runner_gateway/runner_control.proto` (`RunnerControl` and its frames)
  and `agentplane/runner/runner_service.proto` (the `Runner` service), packages
  `agentplane.runner_gateway` and `agentplane.runner`. An incompatible change, a rename included, rides a clean
  cutover: the operator retires every Sandbox on the old protocol, and no incompatible wire version is negotiated.
  Nothing is negotiated: every connection carries commands and lifecycle requests. A frame of a
  kind the receiver does not know fails the stream, and an RPC the service does not implement
  returns `UNIMPLEMENTED`, so a skew shows up at first use rather than as silently dropped work; a
  new frame kind lands in its receiver before any sender uses it.
- **The incarnation.** `Welcome.incarnation_uid` is the UID of the Pod the gateway authenticated, which
  is the Sandbox's current incarnation (a VM's virt-launcher Pod for a VM Sandbox). The runner tells
  its Sessions' seal apart from an earlier incarnation's by it ([teardown seal](../runner/SPEC.md#teardown-seal)).
  A `Welcome` without a UID is rejected before the runner serves calls or quiesces sessions.
- **Epoch fencing.** Reverse calls belong to the authenticated control stream. Forward calls carry
  the epoch from `WatchState`; either path refuses a superseded connection.
- **Message size.** gRPC's default 4 MiB per message.
- **Admissions are facts.** A per-operation RPC answer reports the runner journal. Repeating an
  operation already journaled with the same payload returns its existing `CommandAdmitted` entry,
  which the runner guarantees by deduplicating on `command_id`. The service never infers
  admission from a stream write, a heartbeat or HTTP/2 flow-control progress. Which refusals are
  terminal for a submission is the Dispatcher's decision, not the control stream's; today none is,
  and a refusal leaves it pending. See [Dispatcher admission and runner result](thread_layering.md#dispatcher-admission-runner-result).
- **Status codes.** The service ends a stream with a gRPC status naming the cause:
  `UNAUTHENTICATED`, `PERMISSION_DENIED` (not a Sandbox Pod of this service), `ABORTED` (superseded by a
  newer connection), `INVALID_ARGUMENT` (protocol error) or `UNAVAILABLE` (replica draining or the
  connection's maximum age). The runner logs the status and reconnects, except that
  `PERMISSION_DENIED` backs off to its maximum interval.
- **`Runner` status codes.** A refused call names its cause: `NOT_FOUND` (unknown Session),
  `ALREADY_EXISTS` (another spec, setup or bootstrap script for one already recorded, or a
  `command_id` admitted with another payload), `INVALID_ARGUMENT`, `ABORTED` (the Sandbox is
  sealing or sealed: the same call succeeds once it resumes), `FAILED_PRECONDITION` (the
  Session's journal is sealed, its setup failed, its harness is stopped for a delivery, or it could
  not be started) and `UNAVAILABLE` (the state volume failed; a retry may succeed). Anything else,
  such as a harness that did not launch, is `UNKNOWN`.

## Tunnelled calls

The gateway's stub sits on a channel that never dials: an interceptor sends each call as a `Call`
naming its method, serialized request and remaining deadline, and completes it from the `Reply` with
the same `call_id`. The runner dispatches the generated unary method handler with the remaining deadline and returns
the outcome as a `Reply`. Calls run concurrently and replies come back in completion order.

```protobuf
// agentplane/proto/grpc_tunnel.proto
message Call {
  uint64 call_id = 1;                    // unique among the caller's calls on one stream
  string method = 2;                     // `/package.Service/Method`
  bytes request = 3;
  google.protobuf.Duration timeout = 4;  // remaining deadline; absent for none
}

message Reply {
  uint64 call_id = 1;
  int32 code = 2;                        // gRPC status code; 0 is OK
  string details = 3;
  bytes response = 4;                    // with OK only
}
```

`Connect` keeps owning authentication, fencing and liveness; a call is answered only on the stream
that carried it. When the stream ends the gateway fails its unanswered calls with `UNAVAILABLE`,
since whether they ran is unknown, and a call after it ends fails the same way; a call whose deadline
passes fails with `DEADLINE_EXCEEDED` and its late reply is dropped. A reconnect replays none of
them. Only unary calls are tunneled; forward state observation uses `WatchState`.

## Runner proxy

The gateway replica holding a Sandbox's connection also serves `agentplane.runner.Runner`
on its public gRPC port, which the Sandbox's `<sandbox>-control` Service reaches. The proxy forwards
unary calls through the selected transport and returns the runner's status and response.

- **Sandbox.** The caller names it in the `x-agentplane-sandbox` header, the same name its target
  carries; a gRPC server cannot see the dialed `:authority`. A missing or repeated header is
  `INVALID_ARGUMENT`, an unknown Sandbox `NOT_FOUND`.
- **Expected identity.** Callers must supply `x-agentplane-sandbox-uid`,
  `x-agentplane-sandbox-owner-namespace` and `x-agentplane-sandbox-owner-name` exactly once.
  The proxy checks them against the current Sandbox and ready runner Pod before resolving its
  connection. A stale UID or owner returns `NOT_FOUND`; an unavailable runner returns `UNAVAILABLE`.
- **Authorization.** The bearer is a `token_audience` token. The `caller_accounts` may call every
  method but `Quiesce`, which only `sandbox_controller_account` may; unset, nobody may.
- **Holder.** A replica that does not hold the Sandbox's current connection answers `UNAVAILABLE`
  before acting, and the caller dials the control Service again.
- **Deadline.** The caller's remaining deadline travels in the `Call`; a call without one waits until
  the runner answers or the connection ends.

## Event ingress

The runner pushes to the `ingress` endpoint in its configuration (`runner/transport.go`) and
reports `flushed` from what it recorded. The History
Service serves the ingress (`history_service/ingress/ingress.go`) on a listener of its own, and follows no
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
// agentplane/history_service/runner_ingress.proto
service RunnerIngress {
  rpc Push(stream PushFrame) returns (stream Acknowledgement);
}

message PushFrame {
  oneof frame {
    agentplane.proto.Attached announce = 1;   // first frame, exactly once
    agentplane.proto.EventEntry entry = 2;    // the journal's, verbatim, in cursor order
    agentplane.proto.Sealed sealed = 3;       // terminal
    Heartbeat heartbeat = 4;
  }
}

message Acknowledgement {
  uint64 cursor = 1;                  // every entry through it is committed; never decreases
  bool sealed = 2;                    // the seal at `cursor` is committed
}
```

The ingress is its own service, `agentplane.history_service.RunnerIngress`, apart from `HistoryService`: another server implements
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
   **Deviation:** `Attached.incarnation_uid` and `Sealed.incarnation_uid` name the incarnation that
   announced and the one that sealed. A Session whose seal names another incarnation and whose
   announcement reaches the seal's cursor is reopened: the ingress drops the seal, acknowledges the
   seal's cursor as unsealed and takes entries from the next cursor. The incarnation that sealed gets
   its seal back, and an announcement without a UID never reopens. Every runner seal has a nonempty
   incarnation UID; UID-less seals are invalid.
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
`PERMISSION_DENIED`. Every Sandbox runs as a ServiceAccount named for it, so this ties the Session to its
Sandbox, and the History Service reads the Sandbox off the writer's namespace and name without
resolving it. The Pod is not part of the binding, so a replacement
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
its `SandboxTemplate` sets: one ingress for every Sandbox (the History Service, or a historyless
deployment's own sink), and the control target is always the Runner Gateway. A create request
cannot choose either; an ingress per Sandbox is not built. The runner reaches the ingress over its
egress sidecar's platform route: the sidecar
attaches the Pod's projected token for that ingress's own audience, `agentplane-runner-ingress` for
the History Service, and an audience of its own for a historyless client. No other recipient
can replay a token the ingress saw, and an ingress token opens no control connection.

**Sealing.** `Quiesce` on the control stream seals every journal and returns each Session's seal cursor
with `flushed`, which is true once the ingress has acknowledged that seal. The runner answers from
its recorded acknowledgements, so the report survives a runner restart. Sealing again returns the
same cursors and the current `flushed`. The Sandbox Controller retries an unfinished request at its
one-minute deadline; Lease stage changes enqueue it sooner, and the persisted request time prevents
other watch events or a controller restart from bypassing that deadline. Only after every Session is
flushed may the Sandbox's volume go. A runner that cannot report leaves the Sandbox in sealing for
the operator.

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
    # The runner's last RunnerFacts, each present only while the runner reports it:
    agentplane.allegedly.works/quiesce-stage: draining | flushing | quiesced
    agentplane.allegedly.works/idle-since: <RFC 3339 time the Sandbox became idle; absent while busy>
    agentplane.allegedly.works/bootstrap: running | succeeded | failed | interrupted
    agentplane.allegedly.works/autosuspend-blockers: '[{"reason":"ActiveTurn","sessionId":"<session>"}]'
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

No routing record is kept: runners dial out, so nothing is routed inbound.

- **Accepting a connection.** After authenticating `Hello`, the replica updates the Lease with the
  `resourceVersion` it read: holder itself, transitions plus one, fresh renew time and annotations.
  A conflict means another replica won a race; it re-reads and retries, and the runner's own
  newest-connection rule decides which stream survives. It then writes the EndpointSlice with the new epoch, and only then sends `Welcome`.
  An EndpointSlice write carrying an older epoch than the one stored is skipped.
- **Runner facts.** The holder writes the runner's latest `RunnerFacts`, which arrive on
  `RunnerHeartbeat`, into the Lease annotations with the renewal that records liveness, so the idle
  time adds no write. A change of quiesce stage or bootstrap result triggers that write at once. A
  blocker-only change waits for the regular Lease touch; it does not trigger a separate renewal. The
  Sandbox Controller watches blocker changes and copies them into `status.idleSuspendBlockers` only
  while the Lease is live and names the current incarnation. A new connection starts with none, since
  accepting rewrites the annotations, and the runner republishes after `Welcome`; a runner gone quiet leaves
  its last facts, which a reader trusts only while `pod-uid` names the incarnation it reads and the
  Lease is live. A fact value the gateway does not know ends the stream with `INVALID_ARGUMENT`, so a
  runner publishes a new value only after every gateway accepts it.
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

The Runner Gateway makes Session calls through the control stream. App and Notification callers
reach the holder through the control Service (`control_target`, a required setting).

- `ListSessions` reads retained Session inventory.
- `GetStatus` reads a coherent live snapshot of process incarnations, Session assignments and
  execution status. It never wakes the Sandbox or starts a harness.
- `OpenSession` durably reserves or confirms the immutable Session configuration. Its response
  does not assert native conversation creation or process readiness.
- `SubmitInput` durably admits work before the runner starts/resumes its harness and reconciles
  native evidence. Startup/delivery failures are later journaled outcomes. Caller disconnect does
  not abandon admitted work. Model/effort changes use the same runner-owned preparation path.
- `InterruptTurn` requires the exact active turn. A stale target fails `FAILED_PRECONDITION` before
  admission. An already admitted retry returns its original receipt; native dispatch cannot affect
  a successor turn. It never starts a harness.
- `Quiesce` fences, interrupts active turns, stops harnesses and seals journals. Running Session
  setup and Sandbox bootstrap finish naturally before teardown completes; they are not cancelled.
  It waits for preceding admissions; subsequent new-work calls fail `ABORTED`. Final ingress acknowledgement
  is required for `QUIESCED`.

Only the replica holding the runner's stream calls it. Callers reach that replica through the
Sandbox's control Service. Transport reconnect never replays calls; durable command retry uses the
original command ID. Process identities in diagnostics are per-spawn UUIDs, not command addresses.
Stored native conversations may be dormant while their store's app server is running. A Session
becomes ready after its native conversation preparation/reconciliation, without needing a turn.

| Caller/path                    | Control operation                                   |
| ------------------------------ | --------------------------------------------------- |
| App and Notification inventory | `ListSessions`                                      |
| App Sandbox diagnostics        | `GetStatus`                                         |
| Session Dispatcher opening     | `OpenSession` with the admitted Session ID          |
| Teardown sealing               | `Quiesce`, repeated until journals are acknowledged |
| Dispatcher command delivery    | Matching per-operation Runner RPC                   |

## Prior art and alternatives

Two harnesses already ship a dial-out control channel, and the choices above borrow from both:

- **Claude Code RemoteIO** (`--sdk-url`, [our interoperability spike](../native/claude/x/remote_io/README.md)):
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
   Event Service, [egress spec](../egress/SPEC.md)). Calls into the runner are tunnelled at the
   message level ([Tunnelled calls](#tunnelled-calls)). Running gRPC's HTTP/2 transport itself over
   the stream's bytes, so the gateway would dial the runner through it, needs a custom transport
   that `grpcio` does not expose, and would put every call behind the one stream's flow-control
   window.
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

The runner–Gateway link uses mTLS in both directions. Reverse control dials the Gateway's private
TLS listener directly; it does not use the sidecar's bearer-substituting control route. Forward
control dials the runner's private TLS listener. Neither link accepts a bearer instead of a certificate.
Public Gateway callers still use their existing workload bearers and per-method authorization.

The Gateway's certificate has URI SAN `spiffe://agentplane/runner-gateway`. Each runner certificate
has exactly one URI SAN, `spiffe://agentplane/runner/<namespace>/<sandbox-uid>/<credential-id>`.
Certificates must chain to the configured trust roots and permit the peer's client/server EKU.
A CA-valid certificate with the wrong URI is rejected.

The Sandbox Controller allocates a credential ID before each workload incarnation and cert-manager
issues its certificate into a distinct Secret. The workload carries that ID and its explicit
transport. Gateway authorization checks the current Sandbox UID, current incarnation Pod, owner
chain, transport and credential ID. VM authorization checks the launcher, VMI and Sandbox-owned VM.
An old credential ID never authorizes a replacement workload. Names, addresses and `Hello` fields
are not identity evidence.

cert-manager owns leaf renewal through the namespaced runner Issuer, whose intermediate CA is
signed by the cluster internal CA. trust-manager distributes the cluster internal root bundle.
Pods use projected credential files; guests use a mounted Secret filesystem. Certificate and root
files are reloaded at new TLS handshakes. Peer leaf validity is checked during continuing control
observation, and connections are periodically replaced to pick up rotated credentials and trust.
Active connection age is bounded to one hour; root removal therefore has that bounded delay for
existing connections. Workload deletion or replacement is checked on continuing observation.

The certificate authenticates the Sandbox workload, not every process within it. Code able to read
the runner's private key can act as that workload. No runner certificate authenticates as the
Gateway, and certificates do not replace command authorization or journal deduplication.

### Coordinated transport and authentication cutover

This change requires matching runner, Sandbox Controller, Gateway and template configuration.
Old runner binaries cannot authenticate to the new private listener. Before updating the Gateway,
quiesce and suspend existing runners through the old Gateway and wait for their retained journal
state to flush. Preserve Sandbox objects and state PVCs. Provision the Issuer, Gateway Certificate,
trust bundle and network policy, then deploy the matching services and templates with explicit
transport selections. Resume the Sandboxes after certificates become Ready. Verify
`RunnerConnected`, bootstrap readiness, journal/history continuity and a new admitted command for
each transport in use. A ready Pod alone does not establish control connectivity.

Rollback also requires matching binaries and configuration: quiesce through the working Gateway,
restore the prior Gateway, Controller and template versions together, and resume against retained
state. Do not retry an uncertain command with a new command ID; reconcile the journal admission
receipt first. Certificate resources can remain while rolling back; prior runners do not use them.

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

Existing connection Leases and EndpointSlices must belong to the exact Sandbox API, kind, name and
UID, with no other owners, and must not be deleting. Foreign records, including records of a deleted
Sandbox whose name was reused, block connection acceptance until they are removed.

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

## Per-operation command RPCs

Session Dispatcher durably records the caller's intent, then calls the matching Runner method through
the Sandbox's control Service. The Runner journals each admission with the original command ID and
payload before replying. A repeated operation with the same ID and payload returns the original
`CommandAdmitted` entry; reuse of an ID for another operation or payload is rejected. Dispatcher
tracks delivery status, while History exposes the Runner's journal after it is pushed.

## Heartbeat and liveness

The runner sends `RunnerHeartbeat` and the gateway sends `Heartbeat` on `Connect` every 10 s. Either
side ends the stream after 30 s without any frame from the peer. A runner reconnects with exponential
backoff and jitter, from 0.5 s up to 30 s, and
retries indefinitely. A live connection is not readiness to act: it says nothing about harness
state, an active turn or whether a command was carried out, and losing the connection proves none of
those either. After `Welcome`, the runner sends empty heartbeats while it adopts the named
incarnation. Until adoption succeeds, it sends no Runner facts and answers tunneled calls with
`UNAVAILABLE` without invoking Runner methods.

## Path through the egress proxy

History ingress and other platform routes retain their bearer-substituting egress paths.
Runner control uses its dedicated mTLS connection in either direction and never sends its private
key or control credentials through an HTTP interception proxy.
