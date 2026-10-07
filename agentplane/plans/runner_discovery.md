# Runner discovery and control-plane access implementation notes

Status: **implementation notes for [Sandbox Service](sandbox_service.md); source extraction is implemented, live
authority handoff remains gated.** Kubernetes already describes hosted sandboxes and their runner Pod incarnations.
Discovery belongs inside that backend; neither a separate directory service nor an integration-app lookup API is
required. The [dependency rule](../docs/service_boundaries.md) applies from v1: notifications and other backends must
operate without the integration app.

## Responsibilities

Keep three concepts separate:

- **Authority:** authenticated ServiceAccount/service/operator authority controls resource access. Workloads sharing an
  account have the same ordinary agent authority; no per-session caller identity.
- **Conversation:** an explicitly selected runner session, not a server-inferred app Thread.
- **Delivery binding:** an existing provisioned sandbox reference identifies where the session lives. Lookup resolves
  its current endpoint; it does not grant permission to control it.

[`SandboxInventory`](../sandbox_service/inventory.py) and endpoint resolution live inside Sandbox Service, using neutral
Kubernetes helpers. The app retains [`LiveIndex`](../app/live.py) and [`SandboxSessions`](../app/threads/sessions.py)
only to select explicit service destinations from its UI projections. Do not import those app modules into the service.
The app may still observe provisioning for presentation, but it is not the authoritative endpoint/command path for other
services. Product Thread annotations and UI projections stay app-owned.

## Internal lookup

The conceptual operation is `resolve_runner(destination_ref)`, returning the current runner endpoint and configured
ServiceAccount, temporary unavailability, or authoritative permanent removal. A lookup failure is an error, not any of
those lifecycle facts. This is an internal backend seam, not a new network directory API. Final Python types/names are
not prescribed here.

`destination_ref` names an existing provisioned sandbox with immutable UID and lookup coordinates, not a directory-owned
identity or caller-supplied URL. Normal Pod replacement retaining runner storage changes the endpoint, not the
destination. Name/IP reuse must not redirect subscriptions. Discarding session storage needs an explicit
generation/removal rule; matching names alone do not prove continuity.

The Sandbox Service can establish a self-delivery binding from an authenticated workload's Pod and trusted provisioning
associations. This is routing, not app Thread inference or a replacement for SA access checks. Explicit destinations
must also pass account/access validation. Notifications stores the verified binding, not a guessed endpoint or an app
Thread lookup result.

Session IDs are unique only within the runner's retained state. The proposed examples qualify them with the destination
reference. If choosing an SA-wide session ID instead, establish that uniqueness contract and reject conflicting
bindings; do not pick the first matching runner. Supply the necessary IDs in backend-created agent context, not
exclusively through app prompt construction.

Missing endpoints, watch failures, and timeouts are not permanent removal. Pin UID associations and reject stale
replacements. Limit the service's Kubernetes reads to its configured hosted inventory. Session/removal and
storage-generation rules remain concrete implementation details to settle.

## Existing runner access boundary

The checked-in app client uses `grpc.aio.insecure_channel` in [`RunnerClient`](../runner/client.py), and the server uses
`add_insecure_port` in [`service.py`](../runner/service.py). There is no bearer metadata or application-level RPC
identity verification on that path. The loopback default is not the hosted bind:
[`cluster/cdk8s/agentplane/app.py`](../../cluster/cdk8s/agentplane/app.py) starts the runner on `0.0.0.0:7000`.

Cilium currently supplies the hosted network boundary: app egress permits runner Pods on TCP 7000, and runner ingress
permits Pods in the same namespace, not just the app. Other callers' reachability also depends on egress and the union
of applicable policies. These are source facts, not live enforcement verification. Network reachability control is not
cryptographic RPC authentication or TLS.

## V1: reuse network policy, move the control-plane caller

The extracted Sandbox Service becomes the runner client for migrated operations. App and notifications use its
authenticated/authorized API rather than retaining competing direct runner routes. Deferring runner RPC authentication
does not make the Sandbox Service API unauthenticated.

1. Permit Sandbox Service egress to hosted runner endpoints on TCP 7000.
2. Narrow runner ingress to intended control-plane clients and explicitly audited test/administrative callers. After a
   path is migrated, remove its obsolete app/notification direct access. Prefer namespace-qualified Cilium
   ServiceAccount identity selectors where supported, not labels ordinary sandbox workloads can assign themselves.
3. Audit overlapping policies: a new narrow rule does not undo a broad allow elsewhere. Test allowed and denied peers on
   both source egress and destination ingress.
4. Keep runner RPCs and attachment semantics unchanged. Trusted network-authorized clients retain full protocol/history
   access; no per-command RBAC or receipt-only runner stream is required.

Notifications sends its stable notice command through the Sandbox Service. That service opens the existing runner
session without a spec and relays observed Events; `Attached`, not an address lookup, confirms current harness state. V1
must not implicitly create/resume a session. No app proxy, runner callback, extra directory deployment, or per-session
ticket issuer is required.

## TODO: proper runner authentication and transport security

**Deferred follow-up, not a notification-v1 prerequisite.** Add proper service-to-runner authentication and transport
security consistently for all legitimate runner clients, primarily the Sandbox Service after extraction. Do not build a
notification-specific credential system or confuse network policy with RPC authentication. This supersedes requiring
separate app/notification runner credentials.

Choose and test:

- Caller and runner peer identities over protected transport; mTLS or tokens over TLS are candidates, not selected
  mechanisms. JWT issuance is not part of v1.
- Provisioned trust/credentials, rotation/revocation, and expiry for long-lived attachments.
- Endpoint replacement, wrong-peer rejection, and stale-credential rejection.
- Shared client/server integration with network policy as an additional boundary. Fine-grained runner command RBAC
  remains outside the notification feature.

Until then, the documented boundary is Cilium-enforced access to the existing unauthenticated runner RPC protocol. Do
not claim this TODO is implemented because a connection test succeeds.

## Acceptance

- Extracted lookup/command paths work with the integration app unavailable and without app imports, private tables, API
  endpoints, browser state, or app-only session bootstrap.
- Sandbox Service can reach runners; ordinary sandbox Pods cannot reach other sandboxes' runners, nor can unrelated
  workloads or unneeded direct app/notification paths. Same-Pod loopback remains inside the agreed sandbox trust
  boundary. Exercise overlapping policies and both ingress/egress.
- Endpoint changes retaining state preserve command identity/receipt recovery; stale UID/name reuse and conflicting
  bindings are rejected. Lookup does not create a session or wake a stopped harness.
- Failures and temporary absence preserve subscriptions; permanent cleanup uses authoritative state.
- Notifications uses Sandbox Service for delivery/following, not a second provisioning/control loop.
