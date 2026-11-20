# Environment Runtime control contract

Environment Runtime owns Sandbox identity, bootstrap, external control, Session management,
journals, History delivery and lifecycle coordination with Execution. The control listener serves
Session operations through HarnessHost and Sandbox lifecycle and process diagnostics through
EnvironmentRuntime; an independent Session storage model is deferred.

The control listener serves `HarnessHost` alongside `EnvironmentRuntime`, behind the same peer
and incarnation/boot authorization. HarnessHost commands use `Runtime.admit` and creation uses
`withSessionAdmission`; lifecycle transitions close and drain those admissions.
`EnvironmentRuntime.GetStatus` retains lifecycle, boot ID and harness process diagnostics.
HarnessHost carries `agentplane.harness_host.Session`: active turn, pending
command count, setup and per-Session harness attachment. EnvironmentRuntime serves no Session
creation, command or inventory RPCs. Its diagnostic response has no per-Session rows.

Both HarnessHost inventory RPCs send `x-agentplane-harness-host-session-version: 1` in initial
metadata. A status reader must validate that marker before receiving watch frames; older Runtimes
serve colliding `SessionSummary` wire fields. Environments validates it before reading the upstream
stream and forwards it. Missing or unsupported versions mean unavailable status, never idle.

`HarnessHost.WatchSessions` sends snapshot upserts followed by `CaughtUp`, then changed Session
upserts with a `CaughtUp` checkpoint. Runtime change notifications trigger fresh inventory reads;
it never polls. Intermediate states may coalesce. Every reconnect starts a complete snapshot
and the consumer reconciles missing Sessions only at its initial `CaughtUp`. No resume token is
needed for current-state reconciliation: the host retains Sessions while serving and has no
retained change log to replay. A host removing Sessions must end its watches for resnapshot.

The public wire definitions are [`harness_host.proto`](../../../harness_host/harness_host.proto),
[`environment_runtime.proto`](../control/environment_runtime.proto),
[`session.proto`](../../../proto/session.proto), and
[`environment_runtime_state.proto`](../../../proto/environment_runtime_state.proto).

## Configuration

The [configuration inventory](config.md) specifies every field, source and file: command line,
`control.security` modes, environment resolution and VM resource policy. The
[Runtime SPEC](../SPEC.md#admission-and-component-ownership) names the peers admitted to the control listener.
Session configuration cannot override template resource ceilings or executor selection.

## Initialization

- The Sandbox's launch file (`--launch-file`, written by the Sandbox controller) carries the
  Sandbox's identity and the bootstrap shell source. Environment Runtime starts the bootstrap with
  `/bin/sh -eu` from the configured `initialization_cwd` when Runtime starts; a Sandbox without a bootstrap runs
  nothing. `CreateSession` durably reserves a Session without waiting for the bootstrap. Session setup waits on the Runtime-owned bootstrap component and does not run session setup or start a native harness until the bootstrap succeeds. If it fails
  or is interrupted, admitted commands receive durable failure outcomes.
- The first request durably selects the exact script bytes in the `BootstrapSelections` COSI
  resource at `<state_dir>/runtime-resources.db`, before the script starts. Environment Runtime thereafter refuses
  changed source, including after a failed attempt: two possibly incompatible initializations must
  never be combined in one Sandbox.
- The script runs at most once. An exact request after it ended, by a result of any exit code, only
  replays the record. An exact request that finds the script selected by an earlier Environment Runtime, with
  no terminal event, records the attempt as interrupted and replays that: the script
  may have run any part of itself, and an unknown result does not permit a retry. A fresh
  initialization needs a new Sandbox.
- Stdout, stderr, and the attempt's result or interruption are appended as persistent protobuf
  `BootstrapEvents` COSI resources with dense Sandbox-scoped sequence numbers. Each output chunk is
  retained in full; the final result or interruption event is the durable outcome authority. An
  abandoned `CreateSession` does not stop the script or recording.
- Environment Runtime takes the bootstrap only from its launch file, never over the control protocol.

## Environment Runtime control

- The control listener exposes the standard gRPC health API and reports local startup as `SERVING`
  before state observation begins. The Controller's authenticated `observe_only` `WatchState` request
  supplies the Sandbox UID and current Pod or virt-launcher incarnation UID. Runtime
  creates a fresh boot ID at process start, reserves this UID, and completes incarnation adoption
  before publishing the first facts snapshot. This Controller watch is the sole source that may
  establish the UID; an Environments proxy cannot choose or advance it. The UID remains fixed for this Runtime
  process, and retries after stream loss must name the same UID, including when an adoption attempt
  had an uncertain outcome. Runtime then sends changed facts and a ten-second heartbeat. Internal
  notifications with unchanged facts do not publish extra snapshots.
  Replacing a launcher Pod while preserving its Runtime process (KubeVirt live migration) is
  unsupported: a new launcher Pod is a new incarnation, fenced by incarnation UID and Runtime boot ID.
  The Environments proxy may retry observation independently of Sandbox discovery. Every Environments proxy replica
  observes the same Controller-bound UID independently; cancellation or loss of one observation
  affects only that stream and does not revoke another replica's calls or Runtime-owned work.
- The Sandbox Controller presents `spiffe://agentplane/sandbox-controller` and opens independent
  `WatchState` streams with `observe_only = true`, the Sandbox UID, the current workload
  incarnation UID. Responses contain the Runtime boot ID and facts. This authenticated observation
  establishes the process's incarnation UID and invokes
  adoption through Runtime's ordinary lifecycle barriers before the initial response. A replacement
  workload with retained state can therefore restore its seal. Later passive observers in the same
  Runtime process may reconnect only with that UID; they cannot replace it. Passive streams never
  replace or cancel an Environments proxy watch, and their cancellation cannot revoke Environments proxy control or
  Executor authorization. The Controller cannot invoke control or Executor RPCs, and the Environments proxy
  cannot select passive observation. Explicit insecure fixtures skip peer authentication while
  retaining the request and identity checks.
- Every unary call and execution-session stream requires the exact Sandbox UID, adopted incarnation
  UID and Environment Runtime boot ID. These calls authorize against the Controller-bound
  Sandbox/incarnation/boot identity; their admission and execution contexts survive Environments proxy watch
  loss and same-incarnation observer replacement. Any authenticated Environments proxy replica may make such
  calls. A different Sandbox, incarnation, or boot ID invalidates that authority; the Runtime
  lifecycle and caller stream retain their cancellation rules. Stale boot or incarnation metadata
  fails before admission with `UNAVAILABLE`.
  `Executor.OpenSession` holds one local Codex
  connection per server stream, with typed unary process and file RPCs bound to its session ID.
  Each Environment Runtime admits at most eight open or opening adapter connections;
  this is an Agentplane resource guard, not a Codex session limit. Native frames are bounded to 1 MiB;
  notifications wait for bounded send capacity, so a brief output burst does not close the session.
  JSON-RPC initialization, framing and notifications stay in the Environment Runtime adapter. Native frames
  return as evidence for the sampler's existing journal; the adapter adds no persistence or
  operation receipts. Request IDs correlate calls without promising deduplication. Lost replies
  remain outcome-unknown, and mutations are never automatically replayed. Closing one stream
  releases its connection and detaches its native session. Codex 0.162.1 allows resume for
  30 seconds before expiring that session and terminating its remaining processes. Other
  sessions are unaffected. See the sandbox-owned [Executor API](../execution/SPEC.md).
- `CreateSession` answers with the session's `Attached` snapshot: the spec, setup and harness states,
  `last_cursor`, and the active turn id as of that position. `GetStatus` returns one snapshot with a
  Environment Runtime boot UUID, each harness process (its per-spawn UUID and the Sessions it hosts),
  and the Environment Runtime desired lifecycle and aggregate progress. Lifecycle
  progress includes the desired and published generations, stage, and—when progress matches the
  desired generation—the first unmet barrier captured by the aggregate controller at the predicate
  it is waiting on. The blocker names its COSI resource and process when available; journal blockers
  include the expected seal identity and acknowledgement/seal cursors, and native cleanup blockers
  report pending finalizers. This is a snapshot of the controller's existing barrier evaluation,
  not a second status calculation. Stale progress is shown as last progress without its blocker or
  error.
- Delivered commands are client-chosen and idempotent; the Environment Runtime serializes them with the session
  lock and deduplicates by `command_id`. A delivered command is answered from the journal:
  its `CommandAdmitted` entry, also when the command was already journaled with the same payload,
  even after its harness stopped or its journal was sealed. Otherwise it is refused with its cause:
  an unknown session, a sealed journal, a command ID already used for other work, or a command
  without an ID and an operation. Delivery never creates a session. A newly admitted execution command
  triggers shared background startup; concurrent commands do not wait for it, and the Environment Runtime
  recovers undispatched admitted commands after a process restart without relying on gateway redelivery.
  A command with a durable dispatch marker is not sent again automatically: its native effect may
  already have occurred. Native requests retain their kind and foreign-key links to the commands they carry.
  Recovery reads values from those original commands and replays matching responses already present
  in the Event log. Goose migrates stored request relationships before recovery. Terminal receipt
  facts remain in the Event referenced by the command’s terminal cursor.
  A process-lost active turn also triggers native reconciliation even if its input was already
  confirmed. A lost turn's completion and recovery report precede `HarnessExited`, so readers can
  end at that exit without losing the turn's final evidence. Completed idle sessions remain dormant. Interrupts neither start a harness nor prevent
  recovery required by other work; an admitted interrupt whose target ended is settled as a no-op.
- Each Runtime control call is answered once, with its result or a gRPC status naming the cause.
  `HarnessHost.ListSessions` inventories Sessions; `EnvironmentRuntime.Quiesce` seals them.
  `CreateSession` durably reserves
  a session from its spec; for an existing session with the same spec and setup script when given,
  it confirms the reservation, and any other spec is refused. A `Quiesce` takes effect after every
  command delivered before it is answered; a call after it that starts or resumes work is refused
  at once (see Teardown quiesce), and `ListSessions` waits for it. A lost connection drops
  the answers of calls in flight, never an operation halfway through.

## Firecracker admission pause

- The Sandbox controller first persists a suspension attempt that fences Environments proxy dispatch. It then calls
  `PrepareSuspend` with a positive, increasing generation and the Runtime's exact `idle_since` timestamp.
  The Runtime checks current aggregate blockers and admission counts under its admission lock and closes a
  separate in-memory gate only while still idle. This gate refuses new Session, Execution and native work with
  retryable `UNAVAILABLE`; it does not seal journals, stop processes or change lifecycle mode. Work accepted
  before the gate finishes normally. A repeat for the same prepared generation and timestamp succeeds.
- The gate survives a Firecracker RAM checkpoint and a same-incarnation `WatchState` reconnect. After the
  launcher has actually checkpointed and restored the guest, the controller calls `ResumeAdmission` through
  the newly verified connection. The generation remains as a tombstone: a delayed old prepare cannot pause
  a resumed Runtime, and an old resume cannot release a newer pause. A rejected prepare is also invalidated
  with `ResumeAdmission` before Environments proxy dispatch is reopened. Lost RPC replies are retried with the same
  generation while the persisted attempt keeps dispatch fenced.

## Teardown quiesce

- `Quiesce` is for Sandbox teardown only. The Environment Runtime first records, durably, that the Sandbox is in
  lameduck in this incarnation, naming the incarnation UID from its latest authenticated
  `WatchState`; from then on,
  including after a restart in the same incarnation, it is in lameduck: `CreateSession` and every
  delivered command except an interrupt are refused with `ABORTED`, which a sender reads as "retry
  after the Sandbox resumes". An admitted command whose dispatch marker was not committed is
  terminally failed before the journal is sealed; a command with a dispatch marker stays pending
  because its native effect is uncertain. The Environment Runtime then interrupts each active turn, waits up to a grace period for it to end, and
  stops every harness. Running Session setup and Sandbox bootstrap scripts finish naturally before
  teardown completes, avoiding partially initialized workspaces; their waiters cannot cancel them.
  The turn's `TurnCompleted` has `ended_by_teardown` set, which tells it from a user's interrupt.
  Each session journal is sealed at its final cursor under this incarnation UID. The response reports
  `DRAINING` while work is ending, `FLUSHING` after every journal is sealed, and `QUIESCED` only after
  ingress has durably acknowledged every exact final cursor. Repeated calls report current progress.
- Command admission and lifecycle goals share a Environment Runtime barrier. A command handler already inside
  admission when Quiesce is accepted finishes durably or is rejected before teardown proceeds; new
  ordinary command IDs and new sessions are refused, while an identical command retry retains its
  original admission.
  Runtime closes admission before publishing the next environment desired generation. Component
  controllers publish exact-generation native cleanup; Runtime proves Execution fenced before
  journals seal. Close preserving a seal waits for its exact durable History acknowledgement;
  unsealed Close only drains native work.
- Runtime `Close(ctx)` shares one accepted cleanup worker among callers; caller cancellation ends
  only that wait. Failed native cleanup, engine failure or missing durable sealed acknowledgement
  retains the environment owner. History joins before journals close; the engine and component
  workers join before final owner release.
- Runtime's single COSI engine schedules Session/setup, app-server, bootstrap, Execution and process
  controllers. Their separate observations describe independent progress; the sole lifecycle desire
  is `EnvironmentRuntimeDesiredStates`. Records, journals, acknowledgements and environment seals
  retain their durable authorities. See [lifecycle ownership](lifecycle.md).
- Orderly shutdown waits for accepted setup and bootstrap. Unexpected process loss can interrupt a
  script; recovery records that outcome and never silently reruns it.
- A sealed journal takes no further Events, so its commands are refused. Its ingress push sends
  every entry through the seal's cursor, then `Sealed`.
- Every persisted seal names the nonempty incarnation UID adopted from the Controller's authenticated
  `WatchState`; a seal without that UID is invalid. An unsealed journal has no seal state.
- An ordinary stop never seals, since a stopped session can resume. A seal ends this Sandbox's
  incarnation of the session's history.
- The state volume outlives incarnations, so when `WatchState` adopts an incarnation UID the Environment Runtime
  drops a lameduck recorded under another incarnation UID before it serves any call: a new incarnation
  starts out of lameduck. Journals sealed by the earlier incarnation stay sealed.
