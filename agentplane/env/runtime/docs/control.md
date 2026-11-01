# Environment Runtime control contract

Environment Runtime owns Sandbox identity, bootstrap, external control, and lifecycle coordination
with Harness Host and Execution Service. The current public API still carries both environment and
Session operations; the Task DAG tracks a later split into separate environment and Harness/Session
services. The in-process Runtime-to-Host lifecycle coordinator remains separate from the public API.

The public wire definitions are [`environment_runtime.proto`](../control/environment_runtime.proto),
[`session.proto`](../../../proto/session.proto), and
[`environment_runtime_state.proto`](../../../proto/environment_runtime_state.proto).

## Configuration

Every Pod and VM starts `agentplane-environment-runtime --config PATH --launch-file PATH`. The executable name
has no effect on configuration. Both flags are required; the shared version-2 JSON configuration
rejects unknown fields, unsupported versions and contradictory policy before state ownership.
Environment Runtime serves control at `control.listen_address` with an explicit `control.security.mode`.
`mtls` requires `security.tls`; its server certificate URI must name the Sandbox namespace and UID
in the separate controller-owned launch file. `insecure` is plaintext without peer authentication
and rejects TLS configuration. Missing/unknown modes and bad mTLS credentials never fall back.
Cluster and k3s templates select mTLS; standalone behavior tests may explicitly select insecure. Templates own
paths, endpoints, providers, explicit
environment allowlists, platform instructions, isolation settings, and the configurable Session
reconciliation worker count (default 16). That worker count does not cap Session admission. The
optional `codex_readiness` object configures executor readiness, its probe windows, and app-server
startup windows independently; omitted values retain their defaults. Each explicit value must be
a positive integer millisecond count representable as a Go duration. The launch file contains only
Sandbox identity and optional bootstrap source.

The Pod exec-server companion serves HTTP `GET /healthz` in its existing process for Kubernetes
liveness and readiness checks. Health checks inspect its Unix control socket within one second and
return HTTP 503 when it is unavailable; an unclaimed companion is healthy. Probing never starts
Codex or claims execution ownership.

The loader first resolves Environment Runtime environment declarations, then harness declarations. Each declaration
has inherited names and explicit values; explicit values take precedence. Provider tokens are read
from the named Environment Runtime environment variables. Undeclared Environment Runtime variables never enter native harnesses. VM services additionally receive
systemd-generated service metadata; login-environment defaults are disabled and explicit native
environment values are preserved.
Session configuration cannot override template resource ceilings or executor selection.

Pods and VMs expose `/run/agentplane/config/environment-runtime.json`,
`/run/agentplane/sandbox/launch.json`, `/run/agentplane/config/kubeconfig`, and
`/run/agentplane/trust/` at the same paths. Pod templates mount ConfigMaps; VM boot mounts disks.
The [configuration inventory](config.md) specifies every field, source and file.

The required `environment.kind` selects a Pod or VM variant. Pod policy contains an optional
container executor; VM policy contains console settings, guest/service capacity, block devices and
identity, and an optional systemd-managed executor. This is the only deployment/executor discriminator.
The VM resource policy declares each harness/executor budget once; Environment Runtime derives systemd resource properties
from it. The same typed template value sizes KubeVirt. Pod resources remain Kubernetes-owned.
Omitting the variant's executor selects local tools. Contradictory or partial environment policy
is rejected before environment changes or state ownership.

The VM resource object uses bytes for memory, microseconds per a fixed 100,000 µs CPU
period, process counts for PIDs, and bytes/operations per second for block I/O. The same guest RAM
and vCPU values size the KubeVirt domain. Before the Environment Runtime starts, `ExecStartPre` invokes the
private Go preflight mode, which strictly decodes the shared configuration, validates VM policy,
and confirms the service memory cap is below live `MemTotal`; only then does it apply
`MemoryHigh`, `MemoryMax`, fixed `MemorySwapMax=0`, `CPUQuota` and `TasksMax`. The unit uses
`OOMPolicy=continue` so a child OOM does not stop the Environment Runtime. The preflight limits the shared
`agentplane.slice`, containing the Environment Runtime and separate workload/executor slices. Native services
use their respective Environment Runtime configuration budgets; swap stays disabled and both aggregate and
process cgroups use `memory.oom.group=0`. A cgroup OOM selects individual process victims, so a tool
OOM can leave its native parent running. The kernel chooses victims; harness survival is not
guaranteed. If the native leader exits, Environment Runtime stops its systemd service and proves its entire cgroup empty
before publishing completion or allowing a successor. The enclosing slice
budget remains authoritative across all of its native services.

## Initialization

- The Sandbox's launch file (`--launch-file`, written by the Sandbox controller) carries the
  Sandbox's identity and the bootstrap shell source. Environment Runtime starts the bootstrap with
  `/bin/sh -eu` from the configured `initialization_cwd` when Runtime starts; a Sandbox without a bootstrap runs
  nothing. `OpenSession` durably reserves a Session without waiting for the bootstrap. Host waits on Runtime's bound `WaitBootstrap` RPC and does not run session setup or start a native harness until the bootstrap succeeds. If it fails
  or is interrupted, admitted commands receive durable failure outcomes.
- The first request durably selects the script digest at the fixed path
  `<state_dir>/initialization/request.json`, before the script starts. The Environment Runtime thereafter refuses
  changed source, including after a failed attempt: two possibly incompatible initializations must
  never be combined in one Sandbox.
- The script runs at most once. An exact request after it ended, by a result of any exit code, only
  replays the record. An exact request that finds the script selected by an earlier Environment Runtime, with
  no result and nothing running it, records the attempt as interrupted and replays that: the script
  may have run any part of itself, and an unknown result does not permit a retry. A fresh
  initialization needs a new Sandbox.
- Stdout, stderr, and the attempt's result or interruption are appended to a persistent event log with
  dense Sandbox-scoped sequence numbers. An abandoned `OpenSession` does not stop the script or
  recording.
- Environment Runtime takes the bootstrap only from its launch file, never over the control protocol.

## Environment Runtime control

- Environment Runtime listens on `control.listen_address` and the Gateway dials its `Environment Runtime` and `Executor`
  services with matching explicit security settings. Cluster and k3s deployments use mTLS. The Environment Runtime certificate URI binds the Sandbox namespace, Sandbox UID and credential ID;
  the Gateway presents `spiffe://agentplane/runner-gateway`. Environment Runtime control has no bearer credential.
  Explicit insecure mode skips certificate authentication while retaining all Sandbox, incarnation,
  boot-ID, observer ownership and epoch checks. Fencing metadata supplies no proof of peer identity.
  History ingress remains a separate outbound bearer-authenticated connection.
- The control listener exposes the standard gRPC health API and reports local startup as `SERVING`
  before state observation begins. The Gateway's `WatchState` request supplies the Sandbox UID, current Pod
  or virt-launcher incarnation UID, and Lease connection epoch. Environment Runtime adopts that incarnation
  before publishing its first facts snapshot, then sends changed facts and a ten-second heartbeat.
  Internal notifications with unchanged facts do not publish extra snapshots. The Gateway retries
  a lost observation independently of Sandbox discovery and claims a fresh connection epoch before
  accepting the replacement Environment Runtime boot.
  Cancelling or superseding the watch changes observer readiness but does not cancel already
  admitted durable work.
- Every unary call and execution-session stream is fenced with the Sandbox UID, incarnation
  UID, Environment Runtime boot ID and connection epoch. A stale epoch cannot authorize a call or clear a newer
  observer's readiness. Calls with stale boot or observer metadata fail before admission with
  `UNAVAILABLE`, allowing queued commands to retry through the current observer without marking
  the whole workload incarnation as refusing work. `Executor.OpenSession` holds one local Codex
  connection per server stream, with typed unary process and file RPCs bound to its session ID.
  Each Environment Runtime retains the old relay's budget of eight open or opening adapter connections;
  this is an Agentplane resource guard, not a Codex session limit. Native frames are bounded to 1 MiB;
  notifications wait for bounded send capacity, so a brief output burst does not close the session.
  JSON-RPC initialization, framing and notifications stay in the Environment Runtime adapter. Native frames
  return as evidence for the sampler's existing journal; the adapter adds no persistence or
  operation receipts. Request IDs correlate calls without promising deduplication. Lost replies
  remain outcome-unknown, and mutations are never automatically replayed. Closing one stream
  releases its connection and detaches its native session. Codex 0.162.1 allows resume for
  30 seconds before expiring that session and terminating its remaining processes. Other
  sessions are unaffected. See the sandbox-owned [Executor API](../execution/SPEC.md).
- `OpenSession` answers with the session's `Attached` snapshot: the spec, setup and harness states,
  `last_cursor`, and the active turn id as of that position. `GetStatus` returns one snapshot with a
  Environment Runtime boot UUID, each harness process (its per-spawn UUID and the Sessions it hosts), session
  execution and busy state, and the Environment Runtime lifecycle intent and aggregate progress. Lifecycle
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
- Each `Environment Runtime` call is answered once, with its result or a gRPC status naming the cause.
  `ListSessions` and `Quiesce` answer as the RPCs of the same name do. `OpenSession` durably reserves
  a session from its spec; for an existing session with the same spec and setup script when given,
  it confirms the reservation, and any other spec is refused. A `Quiesce` takes effect after every
  command delivered before it is answered; a call after it that starts or resumes work is refused
  at once (see Teardown quiesce), and `ListSessions` waits for it. A lost connection drops
  the answers of calls in flight, never an operation halfway through.

## Teardown quiesce

- `Quiesce` is for Sandbox teardown only. The Environment Runtime first records, durably, that the Sandbox is in
  lameduck in this incarnation, naming the incarnation UID from its latest authenticated
  `WatchState`; from then on,
  including after a restart in the same incarnation, it is in lameduck: `OpenSession` and every
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
  Runtime closes admission with a new private generation. Host reports native drain completion for
  that binding, Runtime proves Execution fenced, and Host then seals and flushes. A CLOSING intent
  preserves a previously selected seal and waits for its exact History acknowledgement before Runtime
  stops Host. A CLOSING intent without a seal only drains native work.
- Host `Close(ctx)` is local process cleanup. Concurrent callers share cleanup; caller cancellation
  ends only that wait. It never invents an Execution fence or a seal after losing Runtime control.
  Existing durable journal seals remain intact. Lifecycle-engine failure retains the Host lock until
  native cleanup is proven. Runtime keeps the environment lock until Host and Execution are fenced.
- Runtime's in-memory COSI engine schedules Host native Session/setup, app-server and supervised process
  controllers alongside Runtime bootstrap and environment process controllers. Execution
  Service owns its backend lifecycle. Session records, journals and acknowledgements remain durable
  Host authority; the environment seal marker remains Runtime authority. There is no duplicated
  environment controller in Host. See [lifecycle ownership](lifecycle.md).
- Orderly Runtime shutdown also waits for Host setup and Runtime bootstrap. An unexpected process loss
  can still interrupt a script; recovery records that outcome and never silently reruns it.
- A sealed journal takes no further Events, so its commands are refused. Its ingress push sends
  every entry through the seal's cursor, then `Sealed`.
- Every persisted seal names the nonempty incarnation UID from the authenticated `Welcome`; a seal
  without that UID is invalid. An unsealed journal has no seal state.
- An ordinary stop never seals, since a stopped session can resume. A seal ends this Sandbox's
  incarnation of the session's history.
- The state volume outlives incarnations, so on each `Welcome` the Environment Runtime drops a lameduck recorded under
  another incarnation UID before it serves any call: a new incarnation starts out of lameduck. Journals
  sealed by the earlier incarnation stay sealed.
