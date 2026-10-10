# Runner channel

Status: **accepted design, command delivery live on staging canaries.** The Sandbox Service's
channel listener (`sandbox_service/runner_channel.py`), reachable through the egress proxy,
authenticates, fences and keeps connections alive, and delivers dispatch attempts
(`sandbox_service/channel_delivery.py`) for Sandboxes routed to it. The runner dials it when started
with `--channel-target` (`runner/channel.py`); only staging's `runner-channel-canary` template does,
and its commands travel over the channel. Every other runner is reached through the inbound `Attach`
RPC, which Cilium admits only from the Sandbox Service, and every runner's Events still do. This is
the contract the
runner-initiated replacement implements. It covers command delivery, authentication, connection ownership and
dispatch attempts. Event replay over the channel and the remaining lifecycle operations are added
to it later as separate RPCs and do not change what is fixed here.

## Shape

Each runner process is a gRPC client of the Sandbox Service and holds one HTTP/2 connection to
it. On that connection it keeps one long-lived bidirectional `Connect` stream, which carries
command delivery for every Session in its state directory. Each later operation (event following,
lifecycle controls) is its own RPC on the same connection, so HTTP/2's per-stream flow control
keeps a large event catch-up from starving commands. The runner is the client, so service-to-runner
traffic is messages on streams the runner opened, never reverse RPCs. No operation creates a
Session or starts a stopped harness.

```protobuf
service RunnerChannel {
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
  repeated string capabilities = 2;   // e.g. "commands"; later "events", "lifecycle"
}

message Welcome {
  uint64 connection_epoch = 1;
  repeated string capabilities = 2;   // what the service will use on this connection
}

// The runner deduplicates on `command.command_id` within a session, so redelivery is safe.
message DeliverCommand {
  string session_id = 1;
  ducktape.agentplane.protocol.v1.Command command = 2;
}

message CommandReceipt {
  string session_id = 1;
  string command_id = 2;
  oneof outcome {
    // The runner journal's CommandAdmitted entry, with its cursor. Fresh or already journaled.
    ducktape.agentplane.protocol.v1.EventEntry admitted = 3;
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

- **Versioning.** The package is `ducktape.agentplane.runner_channel.v1`; an incompatible change is
  a new package. Additive frame kinds and RPCs are capabilities: a peer uses one only after both
  sides listed it in `Hello`/`Welcome`. A frame of a kind the receiver did not agree to fails the
  stream, and an RPC the service does not implement returns `UNIMPLEMENTED`, so a skew shows up at
  first use rather than as silently dropped work. Rolling either side is safe because capabilities
  are negotiated per connection.
- **Later streams bind to the epoch.** Every RPC other than `Connect` carries the connection epoch
  from `Welcome` in metadata and is refused once that epoch is superseded.
- **Message size.** gRPC's default 4 MiB per message, the same limit the `Attach` path has today,
  so no command that is deliverable now becomes undeliverable.
- **Receipts are facts.** A receipt reports the runner journal. Re-delivering a command already
  journaled with the same payload returns its existing `CommandAdmitted` entry, which the runner
  already guarantees by deduplicating on `command_id`. The service never infers admission from a
  stream write, a heartbeat or HTTP/2 flow-control progress. Which refusal reasons are terminal for
  a submission is the [command admission](../plans/command_admission.md) contract's decision, not
  the channel's.
- **Status codes.** The service ends a stream with a gRPC status naming the cause:
  `UNAUTHENTICATED`, `PERMISSION_DENIED` (not a Sandbox Pod of this service), `ABORTED` (superseded by a
  newer connection), `INVALID_ARGUMENT` (protocol error) or `UNAVAILABLE` (replica draining or the
  connection's maximum age). The runner logs the status and reconnects, except that
  `PERMISSION_DENIED` backs off to its maximum interval.

## Event following

Status: **accepted (operator, 2026-10-10 PDT), not built.** Until it is, every runner's Events are read through
`Attach`.

A runner that lists `events` in `Hello` serves its journal over the channel, and the Sandbox Service
then reads that Sandbox's journals only that way: `FollowSession` for every subscriber, and its own
admission reconciliation. The journal stays the buffer for every subscriber. Nothing is
acknowledged to the runner and the runner deletes nothing on a read; a subscriber's progress is the
cursor it committed, and it resumes from there.

```protobuf
service RunnerChannel {
  rpc Connect(stream RunnerFrame) returns (stream ServiceFrame);
  // Opened by the runner when asked by `OpenFollow`; carries epoch and follow ID in metadata.
  rpc Follow(stream FollowFrame) returns (google.protobuf.Empty);
}

message ServiceFrame {
  oneof frame {
    // ...
    OpenFollow open_follow = 4;
    CloseFollow close_follow = 5;
  }
}

message OpenFollow {
  string follow_id = 1;               // chosen by the service, unique per connection epoch
  string session_id = 2;              // the runner's Session ID
  uint64 after_cursor = 3;
}

message CloseFollow {
  string follow_id = 1;
}

message FollowFrame {
  oneof frame {
    ducktape.agentplane.runner.v1.Attached attached = 1;  // first frame: snapshot and last cursor
    ducktape.agentplane.protocol.v1.EventEntry entry = 2; // cursors strictly increasing by one
    uint64 sealed = 3;                // terminal: journal sealed at this cursor
    FollowRefused refused = 4;        // terminal: unknown Session or cursor beyond the journal
  }
}
```

1. **Opening.** The replica that owns the connection sends `OpenFollow`; the runner opens one
   `Follow` RPC on the same HTTP/2 connection and streams the journal after `after_cursor`, then
   live entries. Each follow is its own HTTP/2 stream, so gRPC's per-stream flow control bounds
   what is in flight: a slow reader stalls its own follow, never `Connect` or another follow. The
   runner reads the journal in pages as the stream drains and holds no unbounded queue. A
   connection carries at most 64 follows; past that, `OpenFollow` is refused and the caller gets
   `UNAVAILABLE`.
2. **Ending.** `CloseFollow`, the subscriber going away, the follow's lease (`follow_lease_s`,
   unchanged) or a superseded epoch ends the follow, and the subscriber resumes from its committed
   cursor. Ending a follow is a transport event only: it never interrupts a turn or stops a harness,
   and a runner never reads a cancelled `Follow` as a command.
3. **Cross-replica.** Subscribers dial the Sandbox's own control Service
   ([Connection records](#connection-records-in-kubernetes)), which resolves to the replica holding
   the runner's connection. A replica asked to follow a Sandbox whose runner stream it does not
   hold refuses with `UNAVAILABLE`, and the subscriber resolves again and retries from its cursor.
   The runner is the fence: it opens `Follow` only on the connection that received its latest
   `Welcome`.
4. **Admission reconciliation.** Each replica's `AdmissionReconciler` takes the channel-routed
   Sandboxes whose Lease it holds and follows their journals locally, from the reconciled cursor,
   advancing it after committing each batch. Positive admission evidence still never regresses.
5. **Errors.** Entries arrive with consecutive cursors; a gap, a regression below the requested
   cursor or a frame after a terminal one fails the follow, and the subscriber's `Store.append`
   refuses a conflicting replay. `sealed` and `refused` map to `FollowSessionResponse.sealed` and a
   `DestinationUnavailable` status.

Rejected for cross-replica follows: a relay between replicas, which adds a hop and an internal
API; a redirect carried in gRPC status details, which every client would have to implement; making
the runner hold a connection to every replica; and passing entries through Postgres, which would
make the Sandbox Service store history.

## Connection records in Kubernetes

Status: **accepted (operator, 2026-10-10 PDT), not built.** It replaces the `runner_connection` table, the
`Connection ownership and fencing` table below and Postgres-based liveness; command submissions
and dispatch attempts stay in Postgres.

Per Sandbox, all in the Sandbox's namespace and owned by the `Sandbox`, so deleting it removes them:

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: <sandbox>-runner
  annotations:
    agentplane.ducktape.dev/pod-uid: <the Pod the connection authenticated as>
    agentplane.ducktape.dev/runner-boot-id: <from Hello>
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
  annotations: { agentplane.ducktape.dev/connection-epoch: "<epoch>" }
addressType: IPv4
endpoints: [{ addresses: [<owning replica's Pod IP>] }]
ports: [{ name: grpc, port: 8080 }]
```

No routing record is kept: once every runner dials out, as the operator decided for a clean cutover
(2026-10-10 PDT), nothing is routed inbound. Agent-sandbox already names a Service after the
Sandbox, hence `-control`.

- **Accepting a connection.** After authenticating `Hello`, the replica updates the Lease with the
  `resourceVersion` it read: holder itself, transitions plus one, fresh renew time and annotations.
  A conflict means another replica won a race; it re-reads and retries, and the runner's own
  newest-connection rule decides which stream survives. It then writes the EndpointSlice with the new epoch, and only then sends `Welcome`.
  An EndpointSlice write carrying an older epoch than the one stored is skipped.
- **Liveness.** The holder renews the Lease with each liveness write. A renew that finds another
  holder or a newer epoch means the connection was superseded, and the replica ends the stream with
  `ABORTED`. A runner counts as connected while `renewTime` is under 30 s old.
- **Reading.** Every replica keeps an informer on Leases in the namespace: `SubmitCommand` reads
  liveness, and the reconciler picks the Leases it holds. No read goes to the API server per request.
- **Stale endpoints.** During a roll, DNS can return a replica that no longer holds the stream for a
  few seconds; it refuses, and the client retries. Nothing reads the endpoint as proof of
  ownership.
- **Costs.** One Lease renewal per connected Sandbox every 15 s and one EndpointSlice write per
  reconnect. The Sandbox Service gains RBAC to write Leases, Services and EndpointSlices in the
  Sandbox namespace. Network policy is unchanged: the endpoints are Sandbox Service Pods.

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
   ledger. Event following still needs cursors and acknowledgements, which the event-replay design
   takes from this model.

## Authentication and incarnation binding

The runner dials the Sandbox Service's channel listener through the Pod's egress sidecar, like
every other destination it reaches; the Sandbox's network fence admits nothing else. It sends a
placeholder in `authorization: Bearer` metadata on every RPC. The egress proxy authenticates the Pod
hop and substitutes that Pod's own projected token for the dedicated audience
`agentplane-runner-channel`, the same mechanism and per-destination audience that
[notifications](../notification_service/README.md) uses. The token is projected only into the
sidecar, so neither the runner nor the harness ever holds it.

The audience is distinct from the Sandbox Service API's (`agentplane-sandbox-service`) and from
the shared `agentplane-egress` audience that the LLM ingress and Action Service receive, so none of
those recipients can replay a token they saw as a runner connection, and a channel token cannot
call the Sandbox Service API.

The rule that admits the channel host lives in its own egress policy, which the Sandbox Service
binds to every Sandbox at creation. It is not one of the launch-selectable
policies, so choosing a narrow egress set cannot cut a Sandbox off from its own control plane.

When a `Connect` stream opens, the Sandbox Service authenticates the bearer with the shared
`WorkloadPrincipalAuthenticator` ([workload authentication](workload_authentication.md)) and then
resolves the principal the way destination resolution does, in reverse:

1. The Pod named by the token exists with the token's UID and is not being deleted.
2. It has exactly one controller reference, a `Sandbox` of the agent-sandbox API, and that Sandbox
   is in the service's inventory with the same UID.
3. The Sandbox runs as the token's ServiceAccount.

A VM environment's runner reaches the channel from its `virt-launcher` Pod's egress relay, and that
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
can open a competing channel connection. That gains nothing it does not already have: it can
already rewrite the runner's journal on the shared volume or kill the runner. The channel
therefore authenticates the Sandbox incarnation and claims no process isolation. Distinguishing
the runner from agent processes needs a resource boundary inside the Sandbox, such as a VM guest
that keeps the runner outside the agent's reach.

## Route selection

The runner says which operations it carries over the channel in `Hello`, and `Welcome` agrees the
ones the service also supports. The Sandbox Service records every operation any connection of the
Sandbox has agreed, and the set only grows: once an operation has been agreed, that Sandbox's
operation is channel-routed for good, whether or not a connection exists right now. Routing is
never inferred per request from whether a connection happens to be live.

For a channel-routed operation with no current connection, the service reports the destination
unavailable. It never falls back to `Attach`, and the inbound route never takes over a command
whose channel delivery was ambiguous. Operations no connection has agreed keep their inbound route,
so a Sandbox can carry commands over the channel while its events are still read through `Attach`,
and an old runner that never dials out keeps working unchanged.

## Connection ownership and fencing

The Sandbox Service keeps one row per Sandbox in its own database:

| column             | meaning                                             |
| ------------------ | --------------------------------------------------- |
| `sandbox_uid`      | primary key                                         |
| `pod_uid`          | the Pod the current connection authenticated as     |
| `connection_epoch` | increases by one for every accepted connection      |
| `owner_replica`    | the service replica holding the current connection  |
| `runner_boot_id`   | from `Hello`                                        |
| `last_seen_at`     | last frame received, written at most every 15 s     |
| `operations`       | every operation a connection has agreed; grows only |

Accepting a connection increments the epoch and sets the owner in one transaction, commits, then
sends `Welcome` with the new epoch. The previous owner, if it still holds a stream, sees its epoch
superseded at its next send check or heartbeat write and ends it with `ABORTED`. A runner keeps at
most one `Connect` stream: it cancels the old one before opening a new one, and acts only on frames from the
connection that received its latest `Welcome`.

Stale sends are harmless rather than impossible. A replica that has not yet noticed it was
superseded can write to a dead stream, and a command that reaches the runner twice is deduplicated
by `command_id`. The epoch decides which replica routes new deliveries and which connection's
heartbeats count; it is not a Session, Event or command identity, and a new epoch neither starts a
harness nor proves that anything failed.

## Dispatch attempts

A retained `pending_admission` submission is not permission to deliver it later. Delivery always
belongs to the submission's bounded **attempt**, kept as columns on the submission row.
`DeliverCommand` and its receipt name the command by the runner's Session ID and the command's own
ID, which the runner already deduplicates on; no separate attempt ID exists.

| column       | meaning                                                                 |
| ------------ | ----------------------------------------------------------------------- |
| destination  | the Sandbox UID and the runner's Session ID; unique with the command ID |
| `deadline`   | the latest submitting RPC's deadline, capped at 60 s                    |
| `sent_epoch` | connection epoch it was last sent on, or none                           |
| `refusal`    | the runner's refusal, or none; admission is the submission's own record |

1. The replica serving the submit RPC persists the submission and opens its attempt, commits, then
   sends `NOTIFY runner_dispatch` with the Sandbox UID, Session ID and command ID.
2. Every replica listens on that channel. The one owning the Sandbox's current epoch claims the
   attempt with a conditional update that succeeds only before the deadline and when `sent_epoch`
   is none or older than its epoch, and sends `DeliverCommand` after that commits. An attempt is
   therefore sent at most once per connection epoch and never after its deadline. No transaction or
   row lock is held across the send.
3. On a receipt the owner records the outcome on the submission in one transaction (positive
   admission evidence never regresses), commits, then sends
   `NOTIFY command_outcome` with the Session ID and command ID.
4. The waiting RPC listens on `command_outcome` before its first state check, then re-reads
   committed state on each notification and every 2 s. While its attempt is open it repeats the
   `runner_dispatch` notification on each check, which also covers a missed notification and an
   owner that changed mid-attempt: the new owner's epoch is newer than `sent_epoch`, so it may send
   again, and the runner answers a duplicate with the existing receipt.

At the deadline the RPC returns without an outcome and the submission stays `pending_admission`. An
expired attempt is not sent again unless a retry reopens it, but a send already underway cannot be
retracted, so a late receipt is still recorded on the submission. A caller retry with the same
command reopens the same attempt: while it is still open to the same Sandbox
the retry only extends the deadline; after a refusal or the deadline it starts afresh, with no
refusal and no `sent_epoch`, so it may be sent again on the current connection. A refusal sent
before the reopen ends the reopened wait too; the submission stays pending either way. Only the
latest refusal is kept. Reconnecting never scans pending submissions for delivery, so a stale
interrupt or stop command is never fired at a reconnecting runner.

Each `Connect` stream carries at most 64 undelivered-receipt attempts; past that, a claim fails and the
attempt waits for its next re-notification.

## Heartbeat and liveness

Each side sends `Heartbeat` on `Connect` every 10 s, and ends the stream after 30 s without any
frame from the peer. A runner reconnects with exponential backoff and jitter, from 0.5 s up to 30 s, and
retries indefinitely. The service reports a runner as connected while its current epoch's
`last_seen_at` is under 30 s old. Connected is not ready to act: it says nothing about harness
state, an active turn or whether a command was carried out, and losing the connection proves none of
those either.

## Path through the egress proxy

A gRPC client behind an HTTP proxy opens a `CONNECT` tunnel, and the egress proxy intercepts the
tunnel to read and substitute metadata. It reads the plaintext HTTP/2 the channel listener speaks
inside that tunnel, and decides a gRPC request from its headers, so the stream's messages pass
through as they are written rather than after the stream closes. Long streams also cross proxy
replica drains, which end them; the runner reconnects like after any other `UNAVAILABLE`.
