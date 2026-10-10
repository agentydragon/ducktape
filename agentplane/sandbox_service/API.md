# Sandbox Service API

The service uses [protobuf/gRPC](protocol.proto). Its standalone entry point serves this API;
HTTP is limited to a health probe. The integration app retains its browser-facing HTTP API.
There is no transparent runner `Attach` tunnel or caller-supplied runner URL.

**Status:** gRPC server/client implementation and acceptance tests are being added in #8744.
Production app callers and deployment source use this API; live rollout is not yet verified.
Service acceptance tests use this same gRPC interface; there is no separate HTTP service adapter.

## Authentication and destinations

Every RPC requires one `authorization: Bearer …` metadata value. The shared workload-principal
resolver performs TokenReview for the configured audience and allowed ServiceAccount namespaces.
Missing, invalid, duplicate, or revoked credentials are refused. The Python client rereads its
projected token file on every RPC, including follow reconnects.

A `SandboxDestination` contains the owner ServiceAccount (namespace/name), Sandbox name, and
Sandbox UID. A `SessionDestination` wraps that destination and adds an explicit runner session ID.
The owner is a resource selector, not a forwarded identity. Every RPC requires a caller in the
configured `caller_accounts` service-account allowlist. Authorization happens before any inventory
lookup or runner contact. An ordinary workload token, even for the destination's own account, does
not grant access. There is no separate manager/delivery tier or inferred Thread identity in v1.
Listed services can operate across owners, but must still supply the verified destination owner/UID.

Resolution checks the stored Sandbox account, UID, operating mode, provisioning/grant facts, and current
ready Pod's identity and controller ownership before selecting its endpoint. Name reuse with a new UID
is refused. A successor Pod under the same Sandbox may replace the endpoint. These are Kubernetes
association checks, not cryptographic runner authentication. **TODO:** proper runner RPC
authentication/TLS; v1 requires network isolation. Service API authentication is implemented
independently of that deferred runner-authentication work.

## Inventory and explicit sandbox lifecycle

The same service-caller allowlist gates every RPC. Provisioning is always enabled:

- `ListSandboxes`, `GetSandbox`: Kubernetes-backed inventory, including concrete stored launch
  bindings, the Sandbox CR's raw status, and a same-name Pod's identity metadata and raw status as
  separate objects. Operating mode, launch-grants-pending, and Kubernetes-grant facts remain
  separate fields; the service does not synthesize one status from them.
- `ListTemplates`: available SandboxTemplates. The app renders its configured grant catalog locally.
- `CreateSandbox`: concrete template, egress policy selections, Action policy sets, Kubernetes grants,
  optional session defaults, and bootstrap. The requested `name` is the Kubernetes Sandbox CR name.
  Retry with the same name and exact choices while that CR exists; a different caller or request
  conflicts. Create first records intent on a Suspended CR, then ensures its UID-owned ServiceAccount
  and grants before resuming. A persisted initialization marker lets reconciliation recover without
  the app. The RPC returns when the CR is persisted; ServiceAccount, grants, and Pod readiness
  are asynchronous.
  After deletion, a new CR under the same name cannot prove whether an earlier Create succeeded.
- `GrantEgress`: UID-pinned Sandbox destination and egress policy names; returns the created binding name.
  `RevokeEgress`: binding name; retains the refusal to delete Git-owned bindings. Both require the
  same service-caller authorization as other operations.
- `SuspendSandbox`, `ResumeSandbox`, `DeleteSandbox`: explicit owner/name/UID-pinned mutations.
  Resume refuses incomplete provisioning; deletion requires suspension.

Create retries by exact caller, name, and request while the CR exists; after deletion no
name-only receipt can prove whether a past Create succeeded. The client disables automatic gRPC
retries. Existing Kubernetes ownership labels, stored bindings, identities, and PVC policy remain
unchanged.

The Sandbox Service controller lists and watches its managed Sandbox CRs from a Kubernetes
`resourceVersion`. It queues changed names, reads the current incarnation for each reconciliation,
and rate-limits failures per name. A startup and periodic full sweep repairs missed events and
orphaned external grants even while the watch is unavailable. Multiple replicas may reconcile the
same name: mutations use UID/resourceVersion guards and are safe to replay. The upstream Sandbox
controller owns `status` and Pod lifecycle; Agentplane's Create intent and progress live in its
namespaced annotations, not competing Sandbox status conditions.

## Sessions and commands

The Sandbox Service raw-history ingester copies only the runner's published,
contiguous Event prefix into the Service database under the existing canonical Session
ID. It verifies the current Sandbox UID/owner before connecting and never sends an Open
spec or starts a harness. Copies from concurrent service replicas may overlap: the
shared store serializes them, accepts exact duplicate bytes and refuses conflicts.
Stopped or deleted Sandboxes retain their already-copied prefix. The ingester always
runs when the Service starts; there is no runtime feature gate. The app's projection
consumes this archive independently. Legacy rows without a known Sandbox UID remain
readable but are not polled until their binding is established by a verified handoff.

- `ListSessions`: Sandbox destination; maps Service-created runner IDs to public Session IDs in
  the returned summaries. Legacy runner-owned sessions retain their existing IDs.
- `OpenSession`: legacy caller-chosen runner ID; retained for deployed app sessions until cutover.
- `CreateSession`: new explicit Open with Sandbox destination, caller-scoped idempotency key,
  and selected overrides (no runner ID). A durable public Session UUID and effective launch
  settings are committed before runner contact; the Service returns the UUID and an attachment
  snapshot with that public ID. A `{session_id}` placeholder in a selected `cwd` override
  is expanded to that UUID after reservation, just like the stored default cwd. Concurrent or response-lost retries use the original settings;
  a changed request with the same key is rejected. `ResumeSession`, `FollowSession`, and
  `SubmitCommand` accept the returned ID and resolve it to the retained runner ID within the
  pinned Sandbox UID. The key is not a Session ID and does not apply to runner-discovered native
  child sessions. No app caller uses this new RPC until its own cutover.
- `LookupSession`: read-only reconciliation by authenticated caller, current Sandbox name/UID
  and opaque Open key. Returns no public ID for an absent reservation, or the public ID
  without runner confirmation for a reserved Open. A runner summary is present only when
  a running native session or its retained `HarnessStarted` Event proves Open succeeded,
  including a session that stopped later. Inventory alone can also contain a failed
  native handshake. A terminal setup/launch failure before any successful start sets
  `failed` without a summary; an in-flight Open stays unconfirmed. The summary includes
  the frozen spec and is intended for the trusted app, **not** for browsers. Lookup
  observes the runner journal without supplying a spec, restarting a harness, or
  rerunning bootstrap.
- Both Open paths: bootstrap
  and setup use the runner's existing idempotence; the response is the native attachment snapshot, not
  a claim that all setup or a model turn has completed.
- `ResumeSession`: uses exactly the runner-retained spec, without applying today's defaults/instructions
  or rerunning setup. Missing sessions and failed/interrupted setup are refused.
- `SubmitCommand`: forwards an unchanged common-protocol `Command` only to a running harness and returns
  the original `EventEntry` containing its exact matching `CommandAdmitted`. Specify a native `Follow`
  cursor before the possible admission when reconciling an uncertain submission.

Read/follow/command RPCs never provision, resume, or wake a Sandbox or harness.

### Launch overrides and field presence

`OpenSessionRequest.spec` and `CreateSessionRequest.spec` use the runner `SessionSpec`. Their
`override_mask` names the exact proto fields to replace in stored defaults, including fields
explicitly set to empty/default values.
For example, `paths: ["model", "instructions"]` selects `spec.model` and `spec.instructions`; an empty
instructions string clears the caller's inherited instructions, but not backend platform guidance.
Nested paths, unknown paths, duplicate paths, and supplied nondefault fields outside the mask are
refused. An empty mask means no overrides. Optional `setup_script` distinguishes omitted from empty.

`platform_instructions` in deployment configuration is the complete deployment-wide platform instruction
block; deployment construction supplies service URLs and assembles all shared guidance into this value.
The Sandbox Service passes it through unchanged. On `OpenSession`, the backend prepends the explicit
Sandbox/session destination (and notification destination when configured), then appends the effective
session `instructions`, stored or overridden. A non-empty `platform_instructions` value is required; there is
no runtime default. Stored specs are never rewritten. For legacy `OpenSession`, changed defaults may make a retry conflict: inspect retained state and
explicitly resume. `CreateSession` instead persists the original effective settings under the caller's
key, so changed defaults cannot change a response-lost retry.

Resume also needs the native harness's retained conversation. The pinned Claude harness can refuse
resuming an empty conversation that never persisted a turn. The service surfaces that refusal; it does
not fabricate native history or claim runner admission proves native persistence.

## Event following

To inspect a session, read the initial `FollowSession` attachment snapshot and cancel the stream.
There is no separate inspection or bootstrap RPC added for tests. Bootstrap runs through `OpenSession`;
exact retries use the runner's stored bootstrap result.

`FollowSession` is server-streaming. Its request selects an explicit session and a native `Follow`
cursor, which may be any cursor of the current Sandbox incarnation's runner journal up to its end.
Entries after it are replayed from the runner journal, then the same stream continues live; the
Sandbox Service serves them from no copy of its own. A cursor beyond the journal's end fails with
`FAILED_PRECONDITION` instead of an `Attached` snapshot. It emits:

1. One native `Attached` snapshot. Entries through its `last_cursor` are replayed, later ones live.
2. Original `EventEntry` messages, preserving serving-log cursor, source origin, and command
   correlation. Replayed and live entries have the same shape and consecutive cursors.
3. An `ended` observation only when the native runner attachment reaches successful EOF. This is a
   transport observation, not a synthesized execution Event or deletion of retained history.
4. Alternatively, a terminal `reconnect_required` observation followed by successful stream closure
   when the follow lease expires, during replay as well as live. This is planned transport renewal,
   not native closure.
5. Alternatively, a terminal `sealed` observation carrying the incarnation's final cursor, after
   every entry through it. It is defined for the runner's teardown seal and not sent yet: no runner
   journals a seal.

Reconnect from the last durably committed cursor; each reconnect rereads the projected token and
checks identity and destination again. The app flushes its buffered batch before planned renewal,
then reconnects immediately, including replay of the archived boundary entry for identity checking.
It does not end the feed. Planned renewals are debug-level observations, not warnings.

Bare service EOF without a terminal observation, `DEADLINE_EXCEEDED`, and backend `UNAVAILABLE`
remain failures, never session termination. The app retries these from the same durable checkpoint
(including when a renewal marker is lost), with warnings for unsuccessful reconnects lasting 30s
and for authorization denial (rate-limited). Invalid history remains a durable feed failure.

Healthy idle follows last until renewal. Each downstream write, including the terminal observation,
is separately bounded by `admission_timeout_s`, so a stalled consumer cannot pin an attachment for
15 minutes. Every exit cancels the runner attachment and closes its channel. There is no unbounded
fan-out queue.

The service now has independent Session Event history storage for durable identity.
`CreateSession` reserves a row but does **not** ingest runner Events yet. Runner logs remain on the
state volume; the app currently retains its own Session Event copy and checkpoints. Backfilling
existing Session Event history and moving Event ingestion/read authority into Sandbox Service are
subsequent cutover work, not part of this Open RPC.

## Errors and uncertain outcomes

- `UNAUTHENTICATED`: invalid workload bearer.
- `PERMISSION_DENIED`: authenticated but unlisted service caller.
- `NOT_FOUND`: missing/stale Sandbox incarnation.
- `INVALID_ARGUMENT`: malformed request or invalid concrete grant selection.
- `FAILED_PRECONDITION`: runner or Sandbox state refuses the operation.
- `UNAVAILABLE`: destination/backend unavailable; no offline admission.
- `DEADLINE_EXCEEDED`: operation or transport safety deadline expired (not planned follow renewal).

Neither a successful write nor a timeout proves admission/rejection. A mutation may commit before
its response is lost. Reconcile commands with the unchanged ID/payload and runner evidence; admission
is neither harness consumption nor command completion nor inbox acknowledgement. No service command
queue, new receipt authority, or exactly-once guarantee is introduced.

## Server configuration and cutover

`//agentplane/sandbox_service:server` uses `AGENTPLANE_SANDBOX_SERVICE_*` environment variables or
kebab-case CLI flags. Required settings are `sandbox_namespace` and
`caller_accounts` (nonempty). TokenReview namespaces are derived from that allowlist.
`token_audience` defaults to `agentplane-sandbox-service`. Kubernetes access is in-cluster unless `kubeconfig` is supplied.

`port` defaults to 8080 for gRPC. `health_port` defaults to 8081 for unauthenticated HTTP `/healthz`;
it is liveness, not proof that Kubernetes or a particular destination is ready. Admission requests
are bounded by `admission_timeout_s` (default 15); `SubmitCommand` instead uses
`runner_admission_ack_timeout_s` (default 15) for runner journal admission. Configure the
app client's `sandbox_service_command_admission_timeout_s` (default 20) above that server budget.
Management uses `lifecycle_timeout_s` (default 300);
follow leases by `follow_lease_s` (default and configured maximum 900 seconds / 15 minutes).
The client whole-follow safety deadline defaults to 960 seconds / 16 minutes; initial attachment
still uses the short request timeout. Renewal repeats TokenReview and destination admission; it
is not in-stream reauthentication or a lease derived from the token's exact expiry. The app's
30-second database ingestion-ownership lease is independently renewed without closing follows.
Runner RPC authentication/TLS remains a separate TODO.

Deployment must grant the service the appropriate Kubernetes/TokenReview/provisioning permissions,
project an audience-correct token for app-to-service calls, and enforce sole normal production access
to runner control/event RPCs. Do not leave a direct-runner app fallback or two provisioning reconcilers.
Inventory/backup and rollback checks gate staging handoff; no staging data reset is part of this change.

Deployment source uses the dedicated `agentplane-sandbox-service` token audience; the app mounts a
rotating projected token and the client rereads it on every RPC. `AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE`
can select an independent YAML settings file. It does not load app configuration or require app startup.

## Representation

Generated protobuf messages are the service's request/response and client types, including inventory,
launch defaults, destinations, and resolved grants. There is no parallel Pydantic service DTO layer.
The integration app owns its browser-facing HTTP schemas and converts only at that boundary.
Pydantic remains for deployment settings, catalog validation, and persisted Kubernetes input parsing;
those are not a second service protocol. Existing annotation spellings and optional-field presence
are retained for staging compatibility and rollback.
