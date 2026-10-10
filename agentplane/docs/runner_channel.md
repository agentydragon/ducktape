# Runner channel

Status: **accepted design, not implemented.** Runners still serve the inbound `Attach` RPC
([runner discovery](../plans/runner_discovery.md)); this is the contract the runner-initiated
replacement implements. It covers command delivery, authentication, connection ownership and
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
  string runner_version = 3;          // image version, for diagnostics only
}

message Welcome {
  uint64 connection_epoch = 1;
  repeated string capabilities = 2;   // what the service will use on this connection
}

message DeliverCommand {
  string attempt_id = 1;
  string session_id = 2;
  ducktape.agentplane.protocol.v1.Command command = 3;
}

message CommandReceipt {
  string attempt_id = 1;
  string session_id = 2;
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
  `UNAUTHENTICATED`, `PERMISSION_DENIED` (not a channel-routed Sandbox), `ABORTED` (superseded by a
  newer connection), `INVALID_ARGUMENT` (protocol error) or `UNAVAILABLE` (replica draining or the
  connection's maximum age). The runner logs the status and reconnects, except that
  `PERMISSION_DENIED` backs off to its maximum interval.

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
binds to every channel-routed Sandbox at creation. It is not one of the launch-selectable
policies, so choosing a narrow egress set cannot cut a Sandbox off from its own control plane.

When a `Connect` stream opens, the Sandbox Service authenticates the bearer with the shared
`WorkloadPrincipalAuthenticator` ([workload authentication](workload_authentication.md)) and then
resolves the principal the way destination resolution does, in reverse:

1. The Pod named by the token exists with the token's UID and is not being deleted.
2. It has exactly one controller reference, a `Sandbox` of the agent-sandbox API, and that Sandbox
   is in the service's inventory with the same UID.
3. The Sandbox runs as the token's ServiceAccount.
4. The Sandbox is channel-routed (below).

A VM environment's runner reaches the channel from its `virt-launcher` Pod's egress relay, and that
Pod resolves to its environment through its own controller chain under the same rules.

Names, IPs, claimed Session IDs and anything in `Hello` are not identity. The connection is bound
to the Sandbox UID and Pod UID that resolution returned. A Sandbox being deleted is still
accepted, because the runner must stay reachable while it seals its Sessions; a deleted Pod fails
TokenReview.

**Revocation.** Every RPC is authenticated when it opens. Each replica watches the Pods and Sandboxes behind the connections it holds, and
ends a connection's streams when its Pod is deleted or replaced or its Sandbox's ServiceAccount changes. As a backstop for a
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

Whether a Sandbox's runner is reached over the channel is fixed for that Sandbox, never inferred
per request from whether a connection happens to exist. The SandboxTemplate declares the operations
its runner image carries over the channel; the Sandbox Service copies that set onto the Sandbox
as an annotation at creation, as it already copies the template's Pod shape (`agentplane.allegedly.works/runner-channel`, for example
`commands`), so editing a template does not move existing Sandboxes. The set grows on an existing
Sandbox only by explicit operator action, one operation at a time.

For a channel-routed operation with no current connection, the service reports the destination
unavailable. It never falls back to `Attach`, and the inbound route never takes over a command
whose channel delivery was ambiguous. Operations not in the set keep their inbound route, so a
Sandbox can carry commands over the channel while its events are still read through `Attach`.

## Connection ownership and fencing

The Sandbox Service keeps one row per Sandbox in its own database:

| column             | meaning                                            |
| ------------------ | -------------------------------------------------- |
| `sandbox_uid`      | primary key                                        |
| `pod_uid`          | the Pod the current connection authenticated as    |
| `connection_epoch` | increases by one for every accepted connection     |
| `owner_replica`    | the service replica holding the current connection |
| `runner_boot_id`   | from `Hello`                                       |
| `last_seen_at`     | last frame received, written at most every 15 s    |

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
belongs to one bounded **attempt**:

| column       | meaning                                                         |
| ------------ | --------------------------------------------------------------- |
| `attempt_id` | primary key, sent in `DeliverCommand` and echoed in the receipt |
| submission   | the admission record (Session destination and `command_id`)     |
| `deadline`   | the submitting RPC's deadline, capped at 60 s                   |
| `sent_epoch` | connection epoch it was last sent on, or none                   |
| outcome      | admitted, refused, or none                                      |

1. The replica serving the submit RPC persists the submission and an attempt, commits, then sends
   `NOTIFY runner_dispatch` with the Sandbox UID and attempt ID.
2. Every replica listens on that channel. The one owning the Sandbox's current epoch claims the
   attempt with a conditional update that succeeds only before the deadline and when `sent_epoch`
   is none or older than its epoch, and sends `DeliverCommand` after that commits. An attempt is
   therefore sent at most once per connection epoch and never after its deadline. No transaction or
   row lock is held across the send.
3. On a receipt the owner records the outcome on the submission and the attempt in one
   transaction (positive admission evidence never regresses), commits, then sends
   `NOTIFY command_outcome` with the attempt ID.
4. The waiting RPC listens on `command_outcome` before its first state check, then re-reads
   committed state on each notification and every 2 s. While its attempt is open it repeats the
   `runner_dispatch` notification on each check, which also covers a missed notification and an
   owner that changed mid-attempt: the new owner's epoch is newer than `sent_epoch`, so it may send
   again, and the runner answers a duplicate with the existing receipt.

At the deadline the RPC returns without an outcome and the submission stays `pending_admission`.
An expired attempt is never sent again, but a send already underway cannot be retracted, so a late
receipt is still recorded on the submission. A caller retry with the same command creates a new
attempt. Reconnecting never scans pending submissions for delivery, so a stale interrupt or stop
command is never fired at a reconnecting runner.

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
tunnel to read and substitute metadata. That interception is proven for TLS upstreams on a
bidirectional stream; the Sandbox Service listens in plaintext. The implementation therefore
either proves plaintext HTTP/2 inside an intercepted tunnel through the real sidecar and proxy,
or serves the channel listener over TLS that the proxy verifies. Long streams also cross proxy
replica drains, which end them; the runner reconnects like after any other `UNAVAILABLE`.
