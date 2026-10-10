# Runner discovery and control-plane access implementation notes

Status: **implementation notes for [Sandbox Service](sandbox_service.md); source extraction is
implemented, live authority handoff remains gated.** Kubernetes already describes hosted sandboxes and their runner Pod
incarnations. Discovery belongs inside that backend; neither a separate directory service nor an
integration-app lookup API is required. The [dependency rule](../docs/service_boundaries.md) applies
from v1: notifications and other backends must operate without the integration app.

## Responsibilities

Keep three concepts separate:

- **Authority:** authenticated ServiceAccount/service/operator authority controls resource access.
  Workloads sharing an account have the same ordinary agent authority; no per-session caller identity.
- **Conversation:** an explicitly selected runner session, not a server-inferred app Thread.
- **Delivery binding:** an existing provisioned sandbox reference identifies where the session lives.
  Lookup resolves its current endpoint; it does not grant permission to control it.

[`SandboxInventory`](../sandbox_service/inventory.py) and endpoint resolution live inside Sandbox
Service, using neutral Kubernetes helpers. The app retains [`LiveIndex`](../app/live.py) and
[`SandboxSessions`](../app/threads/sessions.py) only to select explicit service destinations from
its UI projections. Do not import those app modules into the service. The app may still observe provisioning for presentation, but it is not the authoritative
endpoint/command path for other services. Product Thread annotations and UI projections stay app-owned.

## Internal lookup

The conceptual operation is `resolve_runner(destination_ref)`, returning the current runner endpoint
and configured ServiceAccount, temporary unavailability, or authoritative permanent removal. A lookup
failure is an error, not any of those lifecycle facts. This is an internal backend seam, not a new
network directory API. Final Python types/names are not prescribed here.

`destination_ref` names an existing provisioned sandbox with immutable UID and lookup coordinates,
not a directory-owned identity or caller-supplied URL. Normal Pod replacement retaining runner storage
changes the endpoint, not the destination. Name/IP reuse must not redirect subscriptions. Discarding
session storage needs an explicit generation/removal rule; matching names alone do not prove continuity.

The Sandbox Service can establish a self-delivery binding from an authenticated workload's Pod and
trusted provisioning associations. This is routing, not app Thread inference or a replacement for SA
access checks. Explicit destinations must also pass account/access validation. Notifications stores the
verified binding, not a guessed endpoint or an app Thread lookup result.

Session IDs are unique only within the runner's retained state. The proposed examples qualify them
with the destination reference. If choosing an SA-wide session ID instead, establish that uniqueness
contract and reject conflicting bindings; do not pick the first matching runner. Supply the necessary
IDs in backend-created agent context, not exclusively through app prompt construction.

Missing endpoints, watch failures, and timeouts are not permanent removal. Pin UID associations and
reject stale replacements. Limit the service's Kubernetes reads to its configured hosted inventory.
Session/removal and storage-generation rules remain concrete implementation details to settle.

## Existing runner access boundary

The checked-in app client uses `grpc.aio.insecure_channel` in [`RunnerClient`](../runner/client.py),
and the server uses `add_insecure_port` in [`service.py`](../runner/service.py). There is no bearer
metadata or application-level RPC identity verification on that path. The loopback default is not the
hosted bind: [`cluster/cdk8s/agentplane/app.py`](../../cluster/cdk8s/agentplane/app.py) starts the runner
on `0.0.0.0:7000`.

Cilium currently supplies the hosted network boundary: app egress permits runner Pods on TCP 7000,
and runner ingress permits Pods in the same namespace, not just the app. Other callers' reachability
also depends on egress and the union of applicable policies. These are source facts, not live enforcement
verification. Network reachability control is not cryptographic RPC authentication or TLS.

## V1: reuse network policy, move the control-plane caller

The extracted Sandbox Service becomes the runner client for migrated operations. App and notifications
use its authenticated/authorized API rather than retaining competing direct runner routes. Deferring
runner RPC authentication does not make the Sandbox Service API unauthenticated.

1. Permit Sandbox Service egress to hosted runner endpoints on TCP 7000.
2. Narrow runner ingress to intended control-plane clients and explicitly audited test/administrative
   callers. After a path is migrated, remove its obsolete app/notification direct access. Prefer
   namespace-qualified Cilium ServiceAccount identity selectors where supported, not labels ordinary
   sandbox workloads can assign themselves.
3. Audit overlapping policies: a new narrow rule does not undo a broad allow elsewhere. Test allowed
   and denied peers on both source egress and destination ingress.
4. Keep runner RPCs and attachment semantics unchanged. Trusted network-authorized clients retain
   full protocol/history access; no per-command RBAC or receipt-only runner stream is required.

Notifications sends its stable notice command through the Sandbox Service. That service opens the
existing runner session without a spec and relays observed Events; `Attached`, not an address lookup,
confirms current harness state. V1 must not implicitly create/resume a session. No app proxy, runner
callback, extra directory deployment, or per-session ticket issuer is required.

## TODO: proper runner authentication and transport security

**Deferred follow-up, not a notification-v1 prerequisite.** Add proper service-to-runner authentication
and transport security consistently for all legitimate runner clients, primarily the Sandbox Service
after extraction. Do not build a notification-specific credential system or confuse network policy
with RPC authentication. This supersedes requiring separate app/notification runner credentials.

Choose and test:

- Caller and runner peer identities over protected transport; mTLS or tokens over TLS are candidates,
  not selected mechanisms. JWT issuance is not part of v1.
- Provisioned trust/credentials, rotation/revocation, and expiry for long-lived attachments.
- Endpoint replacement, wrong-peer rejection, and stale-credential rejection.
- Shared client/server integration with network policy as an additional boundary. Fine-grained
  runner command RBAC remains outside the notification feature.

Until then, the documented boundary is Cilium-enforced access to the existing unauthenticated runner
RPC protocol. Do not claim this TODO is implemented because a connection test succeeds.

## Acceptance

- Extracted lookup/command paths work with the integration app unavailable and without app imports,
  private tables, API endpoints, browser state, or app-only session bootstrap.
- Sandbox Service can reach runners; ordinary sandbox Pods cannot reach other sandboxes' runners,
  nor can unrelated workloads or unneeded direct app/notification paths. Same-Pod loopback remains
  inside the agreed sandbox trust boundary. Exercise overlapping policies and both ingress/egress.
- Endpoint changes retaining state preserve command identity/receipt recovery; stale UID/name reuse
  and conflicting bindings are rejected. Lookup does not create a session or wake a stopped harness.
- Failures and temporary absence preserve subscriptions; permanent cleanup uses authoritative state.
- Notifications uses Sandbox Service for delivery/following, not a second provisioning/control loop.

## Outbound control-channel design

**Selected direction and framing; staged implementation, not a deployed migration.**
One runner-initiated gRPC connection per runner process multiplexes Sessions. If its service replica disappears, the runner reconnects
to an available replica. Postgres `LISTEN`/`NOTIFY` provides cross-replica wakeup/routing only; durable
records remain authoritative. Claude RemoteIO inspires the connection direction, not our protocol,
authorization or delivery guarantees.

The [runner channel](../docs/runner_channel.md) design fixes the command-channel contract. Spool
replay and lifecycle frames are designed in their own steps. The following diagram shows the
eventual combined channel, not the first release:

```mermaid
sequenceDiagram
    participant C as Caller
    participant S as Sandbox Service
    participant R as Runner
    R->>S: Authenticate and open outbound gRPC streams
    S->>R: Resume spool after committed archive cursor
    C->>S: SubmitCommand RPC
    S->>S: Persist pending_admission
    S->>R: Command with stable ID
    R->>R: Journal durable admission
    R->>S: Correlated admission receipt
    S->>S: Record admitted
    S->>C: OK with receipt (not execution result)
    R->>S: Independent spool Events
    S->>S: Commit archive prefix and reconcile admissions
    S->>R: Acknowledge committed archive prefix
```

Receipts and spool Events may race; the diagram is illustrative, not a required cross-stream order.
Command insertion and spool listening are independent logical operations on this connection, not
reverse unary gRPC calls. Neither creates a Session or resumes a stopped harness. Define lifecycle
message mappings separately. A replay cursor is not part of command submission identity.

### Coordinated implementation and rollout

The [admission plan](command_admission.md) owns persistence/retry semantics. The sequence intentionally
separates persistence, command transport and event transport rather than making one large migration:

1. `SESSION_COMMAND_CORE` / `SESSION_COMMAND_SUBMISSION`: ship durable admission behind a transport
   interface using the existing `Attach`-based relay and existing service-owned spool reader. The
   adapter can internally use replay to find the receipt; the public durable submission contract
   does not expose that cursor. Reuse is not a new inbound `InsertCommand`/`ListenSpool` rollout.
2. `RUNNER_OUTBOUND_CHANNEL` / `RUNNER_OUTBOUND_CANARY`: implement both command-channel peers. Deploy
   compatible service support first with old routes unchanged, then a compatible runner image in a
   fresh canary. Switch its command adapter to the channel and validate the actual proxy path, cross-replica
   routing, reconnect/fencing and receipts. Keep spool transport unchanged in this stage.
3. `RUNNER_OUTBOUND_SPOOL`: review and add replay/live Events, committed-prefix acknowledgements and
   backpressure; switch the canary reader. Preserve archive identities and duplicate/conflict rules.
4. `RUNNER_OUTBOUND_LIFECYCLE`: migrate remaining lifecycle/inbound consumers. Inventory these early;
   their full wire design need not block the first command canary. Then `RUNNER_OUTBOUND_ROLLOUT`
   makes outbound the default for new Sandboxes; old Sandboxes are archived and deleted rather
   than upgraded, after which inbound access is retired.

Each operation has an explicit route per environment/incarnation. Channel commands plus the legacy spool
reader is deliberate staged coexistence, not two competing command routes. Do not silently fall back
per request or blindly resend ambiguous commands through an old route. Rollback preserves pending
submissions, command IDs, journal/history and exclusive command-route ownership. Remove inbound
access only after all relevant consumers, including lifecycle callers, move.

Implementation remains subject to compatibility verification for this transport change. No
notification metadata, background dispatch or automatic wake is
part of this rollout.

### Channel contract

Framing, versioning, authentication, route selection, connection ownership and fencing, dispatch
attempts and heartbeats are settled in the [runner channel](../docs/runner_channel.md) design. The
runner keeps SQLite command admission and ordered Events; the Sandbox Service authorizes callers
and never infers command success from channel traffic. Moving admission centrally stays a separate,
deferred decision.

### Event replay and flow control

Open for `RUNNER_OUTBOUND_SPOOL`. Define stream acknowledgement versus durable command admission
and durable archive cursor separately. Replay Events from the committed prefix with exact
duplicate/conflict handling; multiple service replicas must not acknowledge data only held in a
lost owner's memory.

Bound Event batches, replay buffers and slow-reader pressure. State whether and
how control traffic avoids starvation during large history catch-up, and define deadlines/cancel
behavior without interpreting transport cancellation as harness cancellation. Heartbeats alone do
not prove that either direction is making application progress.

### VM sequencing and bounded validation

Before `VM_CONTROL_NETWORKING`, state which guest ports,
endpoint discovery and network policies it replaces. Outbound control may simplify VM reachability,
but guest access to LLM/Action APIs still needs its authorized relay. Image/resource-isolation work
can proceed independently; review service/persistence changes as part of their own rollout.

Implement `RUNNER_OUTBOUND_CHANNEL` to the channel design; validate auth denial,
revocation, ordinary reconnect, stale-owner fencing, notification loss and command/receipt races with
automated peers. Add spool replay/backpressure coverage in `RUNNER_OUTBOUND_SPOOL`, not as a gate on
initial admission. Perform a bounded real-VM connection/reconnect/receipt check for the actual
proxy/network path; no compound failure drills. Before VM use without inbound control, complete all
spool and lifecycle capabilities that replace that access. `RUNNER_OUTBOUND_ROLLOUT` then makes
outbound the default for new Sandboxes; existing ones are retired rather than upgraded. Neither is
a gate on first VM use.
