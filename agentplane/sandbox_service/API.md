# Sandbox Service API

The service uses [protobuf/gRPC](protocol.proto). Its standalone entry point serves this API;
HTTP is limited to a health probe. The integration app retains its browser-facing HTTP API.
There is no transparent runner `Attach` tunnel or caller-supplied runner URL.

Production callers and service acceptance tests use this same gRPC interface; there is no separate
HTTP service adapter.

It is two gRPC services, each its own server and image:

- `SandboxService` ([`protocol.proto`](protocol.proto)), served by the Sandbox Service: Sandbox
  inventory and lifecycle, templates, egress grants and [retention holds](#retention-holds). One
  replica reconciles, and it alone holds the RBAC, ServiceAccount and egress grant permissions.
- `RunnerGateway` ([`../runner_gateway/protocol.proto`](../runner_gateway/protocol.proto)), served by
  the [Runner Gateway](../runner_gateway/README.md): Sessions, commands and event following. Its
  replicas hold the runner connections; a Sandbox's `<sandbox>-control` Service reaches the replica
  holding its runner.

`SandboxServiceClient` takes one target per service: `target` for the Sandbox Service and
`gateway_target` for the Runner Gateway.

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
association checks, not cryptographic runner authentication: the inbound runner RPC relies on
network isolation until the [runner control stream](../docs/runner_control.md) replaces it.

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
  The service starts the template's `runner` container with the configured `runner_ingress` and
  Runner Gateway (`runner_control`) as `--ingress-target`/`--ingress-bearer` and
  `--control-target`/`--control-bearer`, and grants each endpoint's `egress_policy` with the launch
  policies ([Event ingress](../docs/runner_control.md#event-ingress)); the request chooses neither.
  A template whose runner already passes one of those flags is refused. No request may pick an
  endpoint's egress policy, at creation or through `GrantEgress`. An unset setting leaves that
  endpoint off.
- `GrantEgress`: UID-pinned Sandbox destination and egress policy names; returns the created binding name.
  `RevokeEgress`: binding name; retains the refusal to delete Git-owned bindings. Both require the
  same service-caller authorization as other operations.
- `SuspendSandbox`, `ResumeSandbox`, `DeleteSandbox`: explicit owner/name/UID-pinned mutations.
  Resume refuses incomplete provisioning. `DeleteSandbox` requires suspension and starts
  [teardown](#teardown).

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

## Teardown

`DeleteSandbox` on a suspended Sandbox returns once teardown has started; `GetSandbox` reports its
`teardown` phase, and repeating the call changes nothing. The controller then:

1. Runs the Sandbox again (`TEARDOWN_PHASE_SEALING`). From now on it refuses new Sessions and
   commands, and suspend and resume; its Sessions can still be listed and followed.
2. Asks the Runner Gateway to seal it (`RunnerGateway.SealSandbox`, through the Sandbox's control
   Service; only the gateway's `sealer_accounts` may call it). The gateway calls the runner's
   [`Seal`](../runner/SPEC.md#teardown-seal), which stops every harness and returns
   each Session's final cursor. A runner image without `Seal` has every running harness stopped
   instead, and each journal's head is taken as its final cursor; only this service's fence keeps
   new work out of it.
3. If the runner pushes its Events to an ingress, deletes the Sandbox, and its volume, once that
   ingress has acknowledged every seal (`flushed`), sealing again every 10 s until then. Holds are
   not consulted: history already has every Session through its seal.
4. Otherwise records the cursors as the [retention holds'](#retention-holds) seal cursors
   (`TEARDOWN_PHASE_SEALED`). A Session is keyed by its public ID, or by the runner's ID for one
   opened with a caller-chosen ID. The Sandbox keeps running, fenced, since a holder confirms its
   hold by following the Session to the seal; one found suspended is run again. It deletes the
   Sandbox, and its volume, once every hold is satisfied.

While the runner is unreachable the Sandbox stays in `TEARDOWN_PHASE_SEALING`, retried with backoff,
for the operator to decide: nothing deletes an unsealed Sandbox.

## Retention holds

A hold says "do not delete this Sandbox until I have its Session through the seal". `PlaceHold`,
`ConfirmHold` and `ReleaseHold` take a `SessionDestination` and a caller-chosen `holder` name, so a
hold belongs to one Session in one Sandbox incarnation; a later incarnation starts without holds.
Any allowed service caller may place, confirm or release any hold, and holds never expire.

- `PlaceHold` is idempotent per Session and holder and keeps an existing confirmation. It is refused
  (`NOT_FOUND`) once the Sandbox's deletion is committed, and (`INVALID_ARGUMENT`) for a Session
  without a seal once the Sandbox is sealed, since it could never be satisfied.
- `ConfirmHold` records the last cursor the holder has durably committed. It never moves backwards,
  so an out-of-order retry is harmless. A missing hold is `NOT_FOUND`.
- `ReleaseHold` is idempotent. An operator clears a stale hold with it.
- A hold is satisfied once its Session has a seal cursor and `confirmed_through >= seal_cursor`.
  Teardown deletes the sealed Sandbox only once every hold is satisfied; a hold on a Session the
  runner did not report keeps it until released. `ListHolds` takes a `SandboxDestination` and lists
  every hold on that incarnation with its confirmed and seal cursors, so it shows which one blocks
  deletion. With no holds, deletion follows the seal.
- A seal ends the Session's incarnation in this Sandbox, not the Session.

The holds are one annotation on the Sandbox CR (`agentplane.allegedly.works/retention-holds`), so
they are deleted with the incarnation they hold. Hold writes and the deletion are both
conditional on the `resourceVersion` they read: a hold placed while a deletion is in flight either
lands first and keeps the Sandbox, or finds the deletion committed and is refused itself.
Each advancing confirmation is a Kubernetes write that the controller and UI informers observe;
only a confirmation at or past the seal changes the deletion gate, so holders need not confirm
every batch.

## Sessions and commands

The Sandbox Service keeps no Session history; the [History Service](../history_service/README.md)
copies each runner's Events and serves reads. Legacy rows without a known Sandbox UID stay
registered but are not followed until their binding is established by a verified handoff.

- `ListSessions`: Sandbox destination; maps every registered runner ID (the public ID itself,
  `r-<uuid>` or an imported legacy one) to its public Session ID in the returned summaries.
  Unregistered runner sessions keep their runner IDs.
- `CreateSession`: explicit Open with Sandbox destination, the client's fresh Session UUID,
  an optional caller-scoped idempotency key, and selected overrides. The UUID is the public
  Session ID and the runner's Session ID alike. The Session and its effective launch settings are
  committed before runner contact; the Service returns the UUID and an attachment snapshot with it.
  A `{session_id}` placeholder in a selected `cwd` override is expanded to that UUID, just like
  the stored default cwd. Concurrent or response-lost retries with the same UUID use the original
  settings; the same UUID with changed inputs, another caller or another Sandbox is rejected. A
  request without a UUID (a client predating them) is reserved by its key alone: the Service mints
  the public ID and gives the runner `r-<public ID>`. `ResumeSession`, `FollowSession`, and
  `SubmitCommand` accept the public ID and resolve it to the retained runner ID within the pinned
  Sandbox UID. Neither the UUID nor the key applies to runner-discovered native child sessions. The
  app opens every new Thread through it, with its Session ID as the key too.
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
- `CreateSession`'s bootstrap
  and setup use the runner's existing idempotence; the response is the native attachment snapshot, not
  a claim that all setup or a model turn has completed.
- `ResumeSession`: uses exactly the runner-retained spec, without applying today's defaults/instructions
  or rerunning setup. Missing sessions and failed/interrupted setup are refused.
- `SubmitCommand`: forwards an unchanged common-protocol `Command` only to a running harness and returns
  the original `EventEntry` containing its exact matching `CommandAdmitted`. For a public Session ID the
  submission is recorded in the `sandbox_commands` database before the runner is contacted, keyed by
  Session and command ID with the caller's identity. An exact retry by the same caller returns the
  retained receipt without contacting the runner once the admission is known; a different command or
  caller under the same ID is `ALREADY_EXISTS`. A submission whose reply was lost is settled only by
  delivering it again: an exact retry sends the same command, and the runner answers a command it
  already journaled with its original receipt. Nothing reads the runner journal or delivers it again in
  the background, so a submission nobody retries stays `PENDING_ADMISSION`. On the inbound relay the
  receipt is found by replaying the journal after `follow`, so the caller names a cursor before the
  command's possible admission; a stopped harness is refused there before any replay. For a legacy
  caller-chosen runner ID nothing is recorded. Once a Sandbox's runner has agreed `commands` on the
  [runner control stream](../docs/runner_control.md), the Sandbox receives its commands only over it,
  through the submission's dispatch attempt. Such a Sandbox takes public Session IDs only, and with no
  live runner connection the call is `UNAVAILABLE` rather than relayed. A runner refusal returns
  `FAILED_PRECONDITION` (`ALREADY_EXISTS` for a conflicting command, `INVALID_ARGUMENT` for an
  invalid one) and leaves the submission pending; an exact retry reopens the attempt and is answered
  with the original receipt even after the harness stopped or its journal was sealed. A recorded
  admission or rejection never changes, and submissions are kept forever: nothing expires or deletes
  them.
- `GetCommand`, `ListCommands`: what the service retained for a public Session's submissions, for any
  allowed caller, without contacting the runner: the command, its state (`PENDING_ADMISSION`,
  `ADMITTED` with the same receipt `SubmitCommand` returns, or `REJECTED` with the runner's refusal),
  and when it was accepted. This is how a caller whose `SubmitCommand` response was lost, or that
  reconnects, learns the outcome; a pending command is settled only by an exact retry or a late
  receipt for a delivery already sent, and `PENDING_ADMISSION` does not say whether the runner
  journaled it. Admission is the runner journaling the command, not the harness acting on
  it. The Session must belong to the named Sandbox incarnation, whose owner is checked while it
  exists; the submissions outlive it. `ListCommands` pages in acceptance order and filters by state.

Read/follow/command RPCs never provision, resume, or wake a Sandbox or harness.

### Launch overrides and field presence

`CreateSessionRequest.spec` uses the runner `SessionSpec`. Its
`override_mask` names the exact proto fields to replace in stored defaults, including fields
explicitly set to empty/default values.
For example, `paths: ["model", "instructions"]` selects `spec.model` and `spec.instructions`; an empty
instructions string clears the caller's inherited instructions, but not backend platform guidance.
Nested paths, unknown paths, duplicate paths, and supplied nondefault fields outside the mask are
refused. An empty mask means no overrides. Optional `setup_script` distinguishes omitted from empty.

`platform_instructions` in deployment configuration is the complete deployment-wide platform instruction
block; deployment construction supplies service URLs and assembles all shared guidance into this value.
The Sandbox Service passes it through unchanged. On `CreateSession`, the backend prepends the explicit
Sandbox/session destination (and notification destination when configured), then appends the effective
session `instructions`, stored or overridden. A non-empty `platform_instructions` value is required; there is
no runtime default. Stored specs are never rewritten. `CreateSession` persists the original effective
settings under the caller's key, so changed defaults cannot change a response-lost retry.

Resume also needs the native harness's retained conversation. The pinned Claude harness can refuse
resuming an empty conversation that never persisted a turn. The service surfaces that refusal; it does
not fabricate native history or claim runner admission proves native persistence.

## Event following

To inspect a session, read the initial `FollowSession` attachment snapshot and cancel the stream.
There is no separate inspection or bootstrap RPC added for tests. Bootstrap runs through `CreateSession`;
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
   every entry through it, once the runner has sealed the Session at teardown. A history copier
   records it as the stored feed state's `sealed`.

Reconnect from the last durably committed cursor; each reconnect rereads the projected token and
checks identity and destination again. The [History Service](../history_service/README.md)'s
ingester, the only production caller, commits its buffered batch on `reconnect_required` and
reconnects at once; renewal does not end the feed.

Bare service EOF without a terminal observation, `DEADLINE_EXCEEDED`, and backend `UNAVAILABLE`
remain failures, never session termination. The ingester logs a warning and retries them from its
committed cursor after its retry interval.

Healthy idle follows last until renewal. Each downstream write, including the terminal observation,
is separately bounded by `admission_timeout_s`, so a stalled consumer cannot pin an attachment for
15 minutes. Every exit cancels the runner attachment and closes its channel. There is no unbounded
fan-out queue.

`CreateSession` reserves the Session's registry row; the Sandbox Service keeps no copy of its
Events. Runner journals remain on the state volume, and the History Service ingests them.

## Session feed

`WatchSessions` is server-streaming and open to every `caller_accounts` caller; it carries no
transcript. It sends every recorded Session, including those of deleted Sandboxes and
reservations whose runner Open never succeeded, in `position` order, then each later change as it
commits on any replica. A `SessionChange` is the Session's current record: public ID, Sandbox name
and current incarnation UID (empty for imported Sessions without one), and that Sandbox's owner
ServiceAccount, which completes the `SessionDestination` for `FollowSession`. The owner is read from
inventory as the change is sent and is unset once the incarnation is gone. A Session that changes again
reappears at a larger position, so a lagging reader sees only its latest record. The feed carries no
harness lifecycle; the Sandbox Service records none, and `FollowSession` reports it.

Positions come from one PostgreSQL sequence allocated under a transaction-scoped lock, so they
become visible in commit order: resuming with `after_position` set to the last processed position
misses nothing. Gaps are normal. Commits wake streams on every replica through PostgreSQL
`LISTEN`/`NOTIFY`; the table stays the authority, and streams re-read after the listener reconnects.
The service does not start without its `LISTEN` connection. Renewal and write bounds are those of
`FollowSession`: `reconnect_required` after `follow_lease_s`, each write within `admission_timeout_s`.

## Errors and uncertain outcomes

- `UNAUTHENTICATED`: invalid workload bearer.
- `PERMISSION_DENIED`: authenticated but unlisted service caller.
- `NOT_FOUND`: missing/stale Sandbox incarnation, or a missing retention hold.
- `INVALID_ARGUMENT`: malformed request or invalid concrete grant selection.
- `FAILED_PRECONDITION`: runner or Sandbox state refuses the operation, including deletion blocked
  by a retention hold.
- `ALREADY_EXISTS`: a `SubmitCommand` command ID already belongs to a different submission.
- `UNAVAILABLE`: destination/backend unavailable; no offline admission.
- `DEADLINE_EXCEEDED`: operation or transport safety deadline expired (not planned follow renewal).

Neither a successful write nor a timeout proves admission/rejection. A mutation may commit before
its response is lost. Settle a command by retrying it with the unchanged ID/payload; admission
is neither harness consumption nor command completion nor inbox acknowledgement. A recorded submission
is not a queue: nothing delivers it later, and the runner journal stays the receipt authority.

## Server configuration and cutover

`//agentplane/sandbox_service:server` uses `AGENTPLANE_SANDBOX_SERVICE_*` environment variables or
kebab-case CLI flags, and `//agentplane/runner_gateway:server` uses `AGENTPLANE_RUNNER_GATEWAY_*`
(`AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` for its YAML file). The Sandbox Service reaches the gateway
at `gateway_target`, and a Sandbox's holder replica through `control_target`, with the token in
`gateway_token_file`; the gateway accepts that account for `SealSandbox` through `sealer_accounts`.
Without `gateway_target`, one Sandbox Service process runs both services and seals in-process; it
also answers the gateway's RPCs under the `ducktape.agentplane.sandbox.v1.SandboxService` name for
clients from before the split. Required settings are `sandbox_namespace` and
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
is not in-stream reauthentication or a lease derived from the token's exact expiry.
Service-to-runner calls are unauthenticated plaintext behind a network policy; the
[runner control stream](../docs/runner_control.md) replaces that route rather than securing it.

`runner_control_port` (default 8082) is a separate gRPC listener serving only the
[runner control stream](../docs/runner_control.md), so a Sandbox's network fence can admit it alone. It
accepts Pod tokens for `runner_control_audience` (default `agentplane-runner-control`), and
`commands_database_url` holds its connection rows next to command admission.

Deployment must grant the service the appropriate Kubernetes/TokenReview/provisioning permissions,
project an audience-correct token for app-to-service calls, and enforce sole normal production access
to runner control/event RPCs. Do not leave a direct-runner app fallback or two provisioning reconcilers.

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
