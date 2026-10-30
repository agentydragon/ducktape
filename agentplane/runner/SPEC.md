# Runner protocol

One gRPC contract over the native Claude Code and Codex harnesses. A caller drives a session
through it without knowing which harness is behind it: the only harness-selection field it ever
sets is `SessionSpec.harness`. The wire definition is [`proto/runner.proto`](../proto/runner.proto); this
page is what the runner guarantees about it.

The runner does not own product Threads, app command persistence, Kubernetes lifecycle, or the
Thread-page projection. Their cross-layer contract is [Thread, runner, and harness layering](../docs/thread_layering.md).

## Configuration

Every Pod and VM starts `agentplane-runner --config PATH --launch-file PATH`. The executable name
has no effect on configuration. Both flags are required; the shared version-2 JSON configuration
rejects unknown fields, unsupported versions and contradictory policy before state ownership.
Runner serves control at `control.listen_address` with an explicit `control.security.mode`.
`mtls` requires `security.tls`; its server certificate URI must name the Sandbox namespace and UID
in the separate controller-owned launch file. `insecure` is plaintext without peer authentication
and rejects TLS configuration. Missing/unknown modes and bad mTLS credentials never fall back.
Cluster and k3s templates select mTLS; standalone behavior tests may explicitly select insecure. Templates own
paths, endpoints, providers, explicit
environment allowlists, platform instructions, isolation settings, and the configurable Session
reconciliation worker count (default 16). That worker count does not cap Session admission. The
launch file contains only Sandbox identity and optional bootstrap source.

The Pod exec-server companion serves HTTP `GET /healthz` in its existing process for Kubernetes
liveness and readiness checks. Health checks inspect its Unix control socket within one second and
return HTTP 503 when it is unavailable; an unclaimed companion is healthy. Probing never starts
Codex or claims execution ownership.

The loader first resolves Runner environment declarations, then harness declarations. Each declaration
has inherited names and explicit values; explicit values take precedence. Provider tokens are read
from the named Runner environment variables. Undeclared Runner variables never enter native harnesses. VM services additionally receive
systemd-generated service metadata; login-environment defaults are disabled and explicit native
environment values are preserved.
Session configuration cannot override template resource ceilings or executor selection.

Pods and VMs expose `/run/agentplane/config/runner.json`,
`/run/agentplane/sandbox/launch.json`, `/run/agentplane/config/kubeconfig`, and
`/run/agentplane/trust/` at the same paths. Pod templates mount ConfigMaps; VM boot mounts disks.
The [configuration inventory](CONFIGURATION.md) specifies every field, source and file.

The required `environment.kind` selects a Pod or VM variant. Pod policy contains an optional
container executor; VM policy contains console settings, guest/service capacity, block devices and
identity, and an optional systemd-managed executor. This is the only deployment/executor discriminator.
The VM resource policy declares each harness/executor budget once; Runner derives systemd resource properties
from it. The same typed template value sizes KubeVirt. Pod resources remain Kubernetes-owned.
Omitting the variant's executor selects local tools. Contradictory or partial environment policy
is rejected before environment changes or state ownership.

The VM resource object uses bytes for memory, microseconds per a fixed 100,000 µs CPU
period, process counts for PIDs, and bytes/operations per second for block I/O. The same guest RAM
and vCPU values size the KubeVirt domain. Before the Runner starts, `ExecStartPre` invokes the
private Go preflight mode, which strictly decodes the shared configuration, validates VM policy,
and confirms the service memory cap is below live `MemTotal`; only then does it apply
`MemoryHigh`, `MemoryMax`, fixed `MemorySwapMax=0`, `CPUQuota` and `TasksMax`. The unit uses
`OOMPolicy=continue` so a child OOM does not stop the Runner. The preflight limits the shared
`agentplane.slice`, containing the Runner and separate workload/executor slices. Native services
use their respective Runner configuration budgets; swap stays disabled and both aggregate and
process cgroups use `memory.oom.group=0`. A cgroup OOM selects individual process victims, so a tool
OOM can leave its native parent running. The kernel chooses victims; harness survival is not
guaranteed. If the native leader exits, Runner stops its systemd service and proves its entire cgroup empty
before publishing completion or allowing a successor. The enclosing slice
budget remains authoritative across all of its native services.

## Initialization

- The Sandbox's launch file (`--launch-file`, written by the Sandbox controller) carries the
  Sandbox's identity and the bootstrap shell source. The runner starts the bootstrap with
  `/bin/sh -eu` from the configured `initialization_cwd` when the runner starts; a Sandbox without a bootstrap runs
  nothing. `OpenSession` durably reserves a Session without waiting for the bootstrap. The runner
  does not run session setup or start a native harness until the bootstrap succeeds. If it fails
  or is interrupted, admitted commands receive durable failure outcomes.
- The first request durably selects the script digest at the fixed path
  `<state_dir>/initialization/request.json`, before the script starts. The runner thereafter refuses
  changed source, including after a failed attempt: two possibly incompatible initializations must
  never be combined in one Sandbox.
- The script runs at most once. An exact request after it ended, by a result of any exit code, only
  replays the record. An exact request that finds the script selected by an earlier runner, with
  no result and nothing running it, records the attempt as interrupted and replays that: the script
  may have run any part of itself, and an unknown result does not permit a retry. A fresh
  initialization needs a new Sandbox.
- Stdout, stderr, and the attempt's result or interruption are appended to a persistent event log with
  dense Sandbox-scoped sequence numbers. An abandoned `OpenSession` does not stop the script or
  recording.
- The runner takes the bootstrap only from its launch file, never over the control protocol.

## Sessions

- A session is one native conversation. Its id is client-chosen, up to 128 characters of
  `[A-Za-z0-9._-]` starting alphanumeric, and the runner keeps its session log under
  `<state_dir>/sessions/<session_id>/`, with Claude's own persistence for a Claude Session. Codex
  persists every Codex Session of the runner in one Codex home, `<state_dir>/codex/`.
- Each Claude Session runs its own harness process. Every Codex Session is one thread of the
  runner's one Codex app-server, started when a Codex Session first needs a harness. Its command
  line and Codex home apply to every Codex Session; each thread carries its own Session's working
  directory, instructions, model, reasoning effort and tool environment, including the notification
  destination. A native frame naming a thread is recorded only in that thread's Session, a
  sub-agent's in its root's; a frame naming none, and the app-server's stderr, is recorded in every
  Codex Session the app-server hosts. Interrupting one Session's turn leaves the others running. An
  app-server exit ends every hosted Session's harness: each records `HarnessExited` and, with an
  active turn, `PROCESS_LOST`, and resumes its thread when it next needs a harness.
- `OpenSession` with an unknown id durably reserves the session from `spec`; `spec.cwd` must be
  absolute. With a known id it confirms the reservation: the `spec` must equal the stored one.
  Opening never starts a harness. Later admitted execution work starts it in the background, and the
  runner returns that command's admission without waiting for startup. A harness that does not
  survive its launch and handshake produces durable launch-failure and command-failure events.
- An unknown session may select one setup script on `OpenSession`. The source is retained with the
  session record so an identical retry can resume a first reservation that failed before
  publication. The runner creates `spec.cwd`, executes the script with `/bin/sh -eu` from that
  directory after bootstrap succeeds, and records started, stdout/stderr, and terminal events in
  the session's durable Event log. `OpenSession` does not wait for setup. The harness starts only
  after setup exits successfully and a command is admitted. After a restart, a setup that was still
  running is recorded as interrupted before the Session is published; its retained source is never
  replayed. A failed or interrupted setup is terminal for that session and is never rerun. A
  different setup source is refused. Start a new session to try again.
- `spec.instructions` are the session's standing instructions: what the session is for, and the
  orders that hold for every turn of it. They reach the model appended to the harness's own system
  prompt, so each harness keeps its coding-agent policy. A caller's empty task leaves the platform part. They are
  fixed for the session's life. The runner composes them from the deployment's platform instructions
  (`runner.json`’s `platform_instructions`), the Session's
  notification destination (the Sandbox identity from the launch file and the Session ID) and the
  caller's task, in that order; callers supply only the task. They are fixed because a `spec` supplied on reopening must equal the stored one.
  Platform guidance supplies deployment service addresses and essential constraints; detailed
  workflows are discoverable through the independent services' `/llms.txt` indexes.
- For a Claude Session the runner asks the workload-authenticated LLM ingress for `ModelConfig` when a
  session or model selection is made. Its `total_context_budget_tokens` is a client
  budget shared by input and output, not a provider maximum or an output request cap.
  The runner sets Claude's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from it; routes without an override use
  the harness's normal metadata. The resolved value is retained in the runner's session record so a
  resume uses the same compaction window. Claude model changes across different resolved windows
  require a new session.
- Codex takes no budget from the runner: its provider names the ingress model catalog
  (`/agentplane/codex/models` on the model endpoint's origin), and each turn uses the entry for the
  turn's model slug, so a model change across windows is allowed and an edited catalog reaches a
  running Codex before its next turn. A route without an entry gets Codex's metadata for an unknown model.
- A session survives the runner process. A runner that starts on a state directory loads every
  session in it; what the previous runner had running is reported as lost (below).
- `ListSessions` returns every session in the state directory with its spec, harness state,
  setup state, active turn, and last cursor, so a client that keeps no record of its own finds them again.

## Isolated Codex execution

- Deployment selects local tools or one isolated executor shared by every Codex Session. Runner
  configures the app-server's `CODEX_EXEC_SERVER_URL`; failed isolated execution never falls back to
  local tools. Claude execution is unchanged.
- In VMs the executor and native harnesses have separate sibling slice budgets under the shared
  `agentplane.slice`. In Pods the executor companion is PID 1 in a separate container with a private PID namespace
  and Kubernetes resource limits. It starts the shared `agentplane-runner` executable in explicit
  `executor` mode, before Runner, COSI and telemetry startup. Workspace and runtime paths are shared
  under the same Sandbox principal.
- Runner starts the executor before the app-server and probes RPC progress every five seconds with
  a three-second deadline. Three consecutive failures fence its work and restart the executor with
  bounded backoff; a successful probe resets the failure count. Server exit fences without waiting
  for further probes. A
  failed tool's side effects remain uncertain; Runner never replays it. Executor loss does not itself
  emit `HarnessExited` for the app-server.
- Startup fences previous executor work before opening Session stores. Quiesce fences the executor
  before journal seals; shutdown fences it before releasing state ownership. VM startup fences prior owned systemd units before opening stores; native services are bound
  to the Runner unit and systemd stops their entire cgroups on Runner death. Pod ownership-connection loss exits the executor
  container, and a new idle container generation proves the runtime cleaned up detached descendants.
  Failed cleanup prevents sealing and starting a successor. A Pod companion that could not spawn
  Codex at all exits no generation and refuses the claim with its reason, staying available to
  fence and to answer a later claim.
- Code-mode workers, MCP helpers and external daemons are outside the executor budget in the pinned
  Codex release. Details and configuration: [Codex executor isolation](docs/codex_executor.md).
- Central sampler execution sessions wait for controller-owned readiness and retries within the
  Gateway's readiness deadline. Closing the session stream cancels its pending readiness wait or connection
  attempt without blocking the Runner control stream. A refused launch or terminal fencing failure
  rejects readiness; it never enables local execution.

## Runner control

- Runner listens on `control.listen_address` and the Gateway dials its `Runner` and `Executor`
  services with matching explicit security settings. Cluster and k3s deployments use mTLS. The Runner certificate URI binds the Sandbox namespace, Sandbox UID and credential ID;
  the Gateway presents `spiffe://agentplane/runner-gateway`. Runner control has no bearer credential.
  Explicit insecure mode skips certificate authentication while retaining all Sandbox, incarnation,
  boot-ID, observer ownership and epoch checks. Fencing metadata supplies no proof of peer identity.
  History ingress remains a separate outbound bearer-authenticated connection.
- The control listener exposes the standard gRPC health API and reports local startup as `SERVING`
  before state observation begins. The Gateway's `WatchState` request supplies the Sandbox UID, current Pod
  or virt-launcher incarnation UID, and Lease connection epoch. Runner adopts that incarnation
  before publishing its first facts snapshot, then sends changed facts and a ten-second heartbeat.
  Internal notifications with unchanged facts do not publish extra snapshots. The Gateway retries
  a lost observation independently of Sandbox discovery and claims a fresh connection epoch before
  accepting the replacement Runner boot.
  Cancelling or superseding the watch changes observer readiness but does not cancel already
  admitted durable work.
- Every unary call and execution-session stream is fenced with the Sandbox UID, incarnation
  UID, Runner boot ID and connection epoch. A stale epoch cannot authorize a call or clear a newer
  observer's readiness. Calls with stale boot or observer metadata fail before admission with
  `UNAVAILABLE`, allowing queued commands to retry through the current observer without marking
  the whole workload incarnation as refusing work. `Executor.OpenSession` holds one local Codex
  connection per server stream, with typed unary process and file RPCs bound to its session ID.
  Each Runner retains the old relay's budget of eight open or opening adapter connections;
  this is an Agentplane resource guard, not a Codex session limit. Native frames are bounded to 1 MiB;
  notifications wait for bounded send capacity, so a brief output burst does not close the session.
  JSON-RPC initialization, framing and notifications stay in the Runner adapter. Native frames
  return as evidence for the sampler's existing journal; the adapter adds no persistence or
  operation receipts. Request IDs correlate calls without promising deduplication. Lost replies
  remain outcome-unknown, and mutations are never automatically replayed. Closing one stream
  releases its connection and detaches its native session. Codex 0.162.1 allows resume for
  30 seconds before expiring that session and terminating its remaining processes. Other
  sessions are unaffected. See the sandbox-owned [Executor API](../executor/README.md).
- `OpenSession` answers with the session's `Attached` snapshot: the spec, setup and harness states,
  `last_cursor`, and the active turn id as of that position. `GetStatus` returns one snapshot with a
  runner boot UUID, each harness process (its per-spawn UUID and the Sessions it hosts), session
  execution and busy state, and the Runner lifecycle intent and aggregate progress. Lifecycle
  progress includes the desired and published generations, stage, and—when progress matches the
  desired generation—the first unmet barrier captured by the aggregate controller at the predicate
  it is waiting on. The blocker names its COSI resource and process when available; journal blockers
  include the expected seal identity and acknowledgement/seal cursors, and native cleanup blockers
  report pending finalizers. This is a snapshot of the controller's existing barrier evaluation,
  not a second status calculation. Stale progress is shown as last progress without its blocker or
  error.
- Delivered commands are client-chosen and idempotent; the runner serializes them with the session
  lock and deduplicates by `command_id`. A delivered command is answered from the journal:
  its `CommandAdmitted` entry, also when the command was already journaled with the same payload,
  even after its harness stopped or its journal was sealed. Otherwise it is refused with its cause:
  an unknown session, a sealed journal, a command ID already used for other work, or a command
  without an ID and an operation. Delivery never creates a session. A newly admitted execution command
  triggers shared background startup; concurrent commands do not wait for it, and the runner
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
- Each `Runner` call is answered once, with its result or a gRPC status naming the cause.
  `ListSessions` and `Quiesce` answer as the RPCs of the same name do. `OpenSession` durably reserves
  a session from its spec; for an existing session with the same spec and setup script when given,
  it confirms the reservation, and any other spec is refused. A `Quiesce` takes effect after every
  command delivered before it is answered; a call after it that starts or resumes work is refused
  at once (see Teardown quiesce), and `ListSessions` waits for it. A lost connection drops
  the answers of calls in flight, never an operation halfway through.

## Event ingress

- The runner pushes every session's journal to the
  [ingress](../docs/runner_control.md#event-ingress) at `ingress.target`, one `Push` call per session until the ingress
  acknowledges the session's seal: at startup, when a session is created, and after a call is lost.
  Each call announces the session's `Attached` snapshot and sends the journal's entries after the
  ingress's first acknowledgement, replayed and then live, at most 1024 beyond the latest
  acknowledgement, then `Sealed` once the journal is sealed.
- The runner records each session's latest acknowledgement on its state volume and deletes no
  journal entry on it, then publishes the acknowledgement to Runner lifecycle observation. Journal
  read/wait, acknowledgement persistence, and lifecycle-observation failures are local and fail the
  Runner; remote stream loss retries with the control stream's backoff. A call the ingress refuses as
  another writer's, or whose first acknowledgement is beyond the journal, retries at the maximum.
  These remote failures do not hold up another session, a command or a lifecycle request: the journal is the buffer.
- The ingress bearer is a placeholder the egress proxy substitutes for the ingress's own audience;
  it is independent of Runner control and never authenticates to the Gateway.

## Teardown quiesce

- `Quiesce` is for Sandbox teardown only. The runner first records, durably, that the Sandbox is in
  lameduck in this incarnation, naming the incarnation UID from its latest authenticated
  `WatchState`; from then on,
  including after a restart in the same incarnation, it is in lameduck: `OpenSession` and every
  delivered command except an interrupt are refused with `ABORTED`, which a sender reads as "retry
  after the Sandbox resumes". An admitted command whose dispatch marker was not committed is
  terminally failed before the journal is sealed; a command with a dispatch marker stays pending
  because its native effect is uncertain. The runner then interrupts each active turn, waits up to a grace period for it to end, and
  stops every harness. Running Session setup and Sandbox bootstrap scripts finish naturally before
  teardown completes, avoiding partially initialized workspaces; their waiters cannot cancel them.
  The turn's `TurnCompleted` has `ended_by_teardown` set, which tells it from a user's interrupt.
  Each session journal is sealed at its final cursor under this incarnation UID. The response reports
  `DRAINING` while work is ending, `FLUSHING` after every journal is sealed, and `QUIESCED` only after
  ingress has durably acknowledged every exact final cursor. Repeated calls report current progress.
- Command admission and lifecycle goals share a Runner barrier. A command handler already inside
  admission when Quiesce is accepted finishes durably or is rejected before teardown proceeds; new
  ordinary command IDs and new sessions are refused, while an identical command retry retains its
  original admission.
  `Close` and `Quiesce` join the same lifecycle: Close preserves a seal UID already selected by
  Quiesce, and a Quiesce accepted during Close adds its seal UID without reopening the Runner. Close
  waits for setup/bootstrap, process stop, outstanding inspection writes and required journal seals,
  then closes journals and releases state ownership. A current-generation teardown error is surfaced
  without reporting the Runner lifecycle complete, and state ownership stays held until native cleanup
  is proven. A failed operation-start publication starts no work, but does not block a later teardown
  generation from running cleanup. Close does not wait for ingress acknowledgement;
  if it supersedes Quiesce before that acknowledgement, the durable seals remain but the Runner can
  close while quiesce progress is still `FLUSHING`.
- `Close(ctx)` is local process cleanup, not an RPC. Concurrent callers wait for the same cleanup;
  a caller's cancellation or deadline ends only its wait, while accepted setup/bootstrap and cleanup
  continue under Runner ownership. Lifecycle-engine exit interrupts waits with its failure cause;
  incomplete cleanup never reports successful Close or releases state ownership. Process termination
  waits for local cleanup independently of the signal context; it does not require ingress acknowledgement.
- One in-memory COSI Runtime per Runner schedules five controller roles: Runner, Session (including
  one-shot setup), Codex exec-server, shared Codex app-server, and Sandbox bootstrap. Controllers own
  lifecycle policy, including readiness probes, restart deadlines and dependency-ordered shutdown;
  native operation hosts retain process handles and publish completion facts. The Runtime mirrors
  input, operation, seal and ingress-acknowledgement facts. It owns no native handles or durable truth. The Session
  store, journal, persisted ingress acknowledgement and state-owner lock remain authoritative across
  restart. See [Runner lifecycle ownership](docs/lifecycle.md) for the controller/resource map and
  shutdown ordering.
- Orderly runner shutdown also waits for running setup and bootstrap. An unexpected process loss
  can still interrupt a script; recovery records that outcome and never silently reruns it.
- A sealed journal takes no further Events, so its commands are refused. Its ingress push sends
  every entry through the seal's cursor, then `Sealed`.
- Every persisted seal names the nonempty incarnation UID from the authenticated `Welcome`; a seal
  without that UID is invalid. An unsealed journal has no seal state.
- An ordinary stop never seals, since a stopped session can resume. A seal ends this Sandbox's
  incarnation of the session's history.
- The state volume outlives incarnations, so on each `Welcome` the runner drops a lameduck recorded under
  another incarnation UID before it serves any call: a new incarnation starts out of lameduck. Journals
  sealed by the earlier incarnation stay sealed.

## Events

Every observation is delivered in a common `EventEntry`: the runner's session log assigns a dense
cursor from 1 and a stable source id plus source sequence in its `origin`. Cursors order a
particular serving log; an importer may assign a different cursor without changing the origin or
event. The event has a timestamp. Derived events name the `Native` events they came from in
`source_sequences`; the harness frames themselves are delivered verbatim, in both directions, so
harness-native detail is one lookup away.

The journal records `TurnCompleted` only when its turn id matches the active-turn checkpoint;
stale and duplicate completion notifications are no-ops. A native frame that carried one remains
retained verbatim as `Native`.

| Family  | Events                                                                                                                                       |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| harness | `HarnessStarted` (resumed, pid), `HarnessExited` (exit code, stopped by the runner), `HarnessLost`, `HarnessStderr`                          |
| command | `CommandAdmitted`, `CommandFailed`, `CommandNoop`, `HarnessUserMessageConfirmed`, `ModelChanged`                                             |
| turn    | `TurnStarted`, `TurnCompleted` (`COMPLETED`, `INTERRUPTED`, `FAILED`, `PROCESS_LOST`)                                                        |
| item    | `ItemStarted` (assistant text, reasoning, tool call), `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`, `ToolOutputDelta`, `ItemCompleted` |
| native  | `Native` (direction, exact line)                                                                                                             |

Connection-loss behavior pinned by `native/{claude,codex}/test_connection_loss.py` and
`runner/test_turns.py::test_model_connection_loss_through_runner`: with the runner driving the
real harnesses against scripted model endpoints, both survive native retry exhaustion before
content as one admitted input and one `FAILED` turn, then accept another input successfully.
After a visible partial text delta and lost stream, both harnesses can retry successfully;
Claude switches to a non-streaming model request for that retry. These tests do not establish
HTTP-error behavior or app archive/UI presentation of failures.

Items are the units of assistant output within a turn. Text and reasoning items stream `TextDelta`
and complete with their full text; tool calls stream their arguments where the harness does,
report the complete `ToolArguments`, stream output where the harness does, and complete with the
harness's outcome. Tool names and argument shapes are the harness's own.

## Commands and effects

- A `Command` is client-chosen and idempotent. The runner commits the full command and its
  `CommandAdmitted` Event together. Admission means the runner owns trying to process that exact command;
  it does not claim a native effect or a scheduling category. Malformed commands are transport
  errors and have no admission event.
- A later causal event is the terminal result: `HarnessUserMessageConfirmed`, `ModelChanged`, an
  interrupted `TurnCompleted`, `CommandFailed`, or `CommandNoop`. Re-submitting an already
  journaled command id does not dispatch it again. Restart recovery is a separate boundary:
  it replays committed Events unchanged and reconciles nonterminal commands with their original ids.
  Native execution before durable effect evidence is not proven duplicate-free across restart, so a
  dispatch-planned command without terminal evidence remains pending instead of being resent.
- `SubmitInput` is terminal only when the harness confirms its causal delivery. Its receipt names
  the native message/correlation id, text, turn id, and every originating command id. Claude's
  normal request carries its representative input UUID on `stream_event.message_start`; compatible
  queued inputs may be newline-joined under that UUID, with bookkeeping replay frames for their
  followers. While a Claude tool is active, later inputs instead join the tool-result continuation
  and Claude emits neither a user echo nor an input UUID: the runner reports the tool-result id plus
  the exact following `started` cohort as the causal evidence. Codex reports each accepted
  `turn/start` input separately.
- `InterruptTurn` is terminal when the named turn reports `INTERRUPTED`, whose
  `interrupted_by_command_id` names the command. If the turn is already over it is a `CommandNoop`.
- `ChangeModel` has harness-specific native effects. Claude sends `set_model` and reports
  `ModelChanged` after its successful control response. Codex retains the request until a later
  `turn/start` response proves that selected model was used; a superseded pending request is a
  `CommandNoop`. Neither harness's behavior is represented as a public "now" or "at boundary"
  choice.
- A Claude model change is rejected with a new-session diagnostic when the current and requested routes
  have different configured context windows, including a transition between a configured and
  unlisted route. Same-window changes and changes between unlisted models keep their existing
  behavior. This keeps the per-process context and compaction limit aligned with the route for the
  lifetime of its native thread; start a new session to use a different window.

## Durability and restart

- One runner has exclusive lifetime ownership of one retained `state_dir`. It takes a nonblocking
  POSIX exclusive lock on the fixed `<state_dir>/.agentplane-runner-owner` inode before it reads
  sessions, opens a journal, dials out, runs initialization, or starts a harness. A contender that
  cannot take that lock exits without serving the directory. The normal runner shutdown releases
  ownership only after its sessions stop and their journals close.
- Production Pods explicitly select PID1 supervision and reject startup outside container PID 1.
  Runner directly spawns natives in process groups and retains state ownership through cleanup.
  One reaper owns direct-child exit statuses and adopted orphans; native leaders never compete
  with an `exec.Cmd.Wait` goroutine. Leader exit kills its remaining group; completion waits until
  the group disappears. Stop sends TERM, then KILL after its grace budget. Non-PID1 fixtures
  explicitly select the internal subprocess supervisor, whose parent-liveness pipe and trusted
  owner descriptor fence the native group after Runner death.
  Both Pod backends require observed container-runtime exit to fence escaped process groups and
  natives that close inherited descriptors. A lock acquisition or force-deleted Pod's API
  disappearance does not prove that outer fence. Replacement after node loss or force deletion
  requires a prior-container or external node/storage fence.
  [Supervision](docs/supervision.md#pid-1-and-the-outer-fence) describes these boundaries.
- In VMs every native launch is a systemd transient `Type=exec` service, bound to the Runner unit,
  with `Restart=no` and five-second stop grace. A condition requires the current Runner invocation's
  marker in its systemd-managed runtime directory; delayed requests from a previous invocation
  cannot execute after replacement. Systemd contains descendants across process-group changes.
  Runner reports completion only after the service is inactive/failed and its execution
  domain is empty. Native services do not inherit the state-owner descriptor: a replacement may
  acquire the lock after Runner death, but it must discover and fence the prior units in its
  explicit Sandbox/state owner scope before opening Session stores, journals or admitting native
  work. It verifies each unit's expected slice and leaves unrelated units alone. A lost or
  canceled launch reply is resolved by fencing that same launch identity, never by issuing a
  second launch. A status-read timeout on a connected manager retries without declaring native
  exit or releasing ownership. Manager disconnection remains a fencing barrier, preventing
  successors and seals until the native domain is proven empty.
  [Supervision](docs/supervision.md) describes the resource hierarchy and backend boundary.
- The supported state mount is the checked-in node-local `piraeus-hdd-local` PVC. A remote or
  otherwise unreliable locking filesystem is unsupported rather than a substitute cross-host
  fencing authority.
- A command admission and its public Event commit atomically. A terminal outcome and its public
  Event also commit atomically, including every origin of a coalesced user message. Recovery
  retains their exact payloads and association; it does not reconstruct missing receipt Events.
- Every public Event, including native frames and streaming deltas, crosses the runner storage
  durability fence before it becomes available to followers. Reopening the surviving state volume
  preserves the published prefix with the same payloads and cursors. Session metadata and journal
  filenames are persisted before the session publishes Events. This relies on the filesystem and
  storage honoring successful synchronization; it does not cover destruction of the state volume.
- A failed or cancelled journal transaction publishes no new Event and prevents further appends
  through that writer. Followers see an error after their recorded prefix; recovery requires
  reopening the journal. The interrupted transaction may or may not survive, but cannot reuse a cursor
  already published for another Event. Harness output recording failures stop the harness.
- Journal writes wait up to five seconds for SQLite locks held by external readers, including online
  backups. A failed commit rolls back its transaction to release locks while keeping the writer failed;
  this does not resume execution or publish any uncommitted Events.
- A runner that finds a session it had running reports `HarnessLost`, then `TurnCompleted` with
  `PROCESS_LOST` if a turn was active. It replays committed Events unchanged and automatically
  starts the harness in the background to reconcile remaining admitted commands. A dispatched command
  with no terminal evidence remains pending; a missing response is not a failure or proof that retry
  is safe. Command dispatch returns after the native write; correlated response Events advance the
  command state and release later response-dependent commands. If a response was committed before the
  restart, recovery translates it and settles the command. A planned interruption without terminal
  evidence also remains pending because the runner cannot know whether it reached the harness.
  Unanswered Codex requests recovered from the journal do not block fresh input; their commands
  remain pending and are never resent. Newly sent requests gate later input until their responses
  are recorded.
- A runner that finds a session whose setup had started without a terminal event records
  `SetupInterrupted`. It never executes that script again. The Pod container-runtime fence or VM startup fencing barrier prevents a replacement
  from opening journals or running work while a surviving setup child remains.
- A harness that exits on its own is reported the same way, as `HarnessExited` with the exit code
  instead of `HarnessLost`.
- A runner that receives SIGTERM stops every running harness without interrupting an active turn
  first. Pods close stdin, then use SIGTERM and SIGKILL with five seconds per step. VMs close stdin
  and request systemd stop, with five seconds after SIGTERM before SIGKILL. Runner records `HarnessExited` with `stopped_by_runner`, then exits. Whatever supervises the
  runner must allow it at least twenty seconds before killing it; a harness killed outright is `HarnessLost` on the next start instead.

## Interrupted turns and continuation recovery

The runner reports `ConversationReconciled` after an ordinary interruption and after native
resume, before delivering subsequent input. It covers the most recent completed turn and any
later interrupted turns on resume; an ordinary interruption covers that turn. Each previously
observed item is `RETAINED`, `ABSENT`, `REVISED` (with its continuing content), or `UNKNOWN`
(with a reason). These are point-in-time statements about continuation, not guarantees against
future compaction. Native interpretation belongs to the runner adapter; clients consume the
same dispositions for every harness.

Codex reconciliation requires `paginated` history, checked from `thread/start` or `thread/resume`.
It pages `thread/items/list` with a native `turnId`, at most 100 items per response. An ordinary
interruption reads that turn; resume reads the most recent completed turn and later turns from the
runner journal. An item that finishes in a later turn adds its original owning turn to the reads.
An entirely unobserved window needs no RPCs. When validating earlier items, later turns still
need a scoped compaction check even if their items were not observed. One history snapshot serves
all turns of a resume. There is no private
Codex file/database reader or legacy-format fallback. Unsupported history, an incomplete/invalid
page sequence, or a compaction marker in the inspected window produces `UNKNOWN`. Unsupported tools
and command executions without a fully observed completed result also remain `UNKNOWN`; API
display output cannot establish the synthetic model continuation of an interrupted command.

Interruption reconciliation runs outside native stdout translation, so the shared Codex reader can
deliver history responses. Subsequent input waits until the reconciliation report is committed.

Process loss first ends the active turn as `PROCESS_LOST` and reports its items `UNKNOWN`.
Explicit resume replaces those decisions using surviving native evidence. An unreadable or
unsupported native history stays `UNKNOWN`; absence of evidence does not establish `ABSENT`.
Unknown recovery does not prevent the user from continuing, but it provides no context guarantee.

Observed text, arguments, outputs, and execution results remain in the event archive. A fold
keeps absent/unknown items with their disposition; revised content becomes the item's current
payload revision. Reconciliation never marks a tool successful or failed, undoes a known result,
or reissues an old call. Losing a tool's context does not mean its side effects were undone, and
receiving synthetic interruption content does not prove how execution ended.

`//agentplane/app:test_bridge` checks streaming text, active tool execution, and a completed tool
followed by streaming text, each interrupted normally, stopped/resumed, and killed/resumed.
Both native harnesses run against asserting model endpoints. Tests compare recovery dispositions
and materialized Thread payloads with the next model request, and verify that recovery does not
repeat a shell side effect. This does not establish exact equality for harness-private context,
reasoning, compaction, or unmodeled tools.

`//agentplane/runner:test_restart` additionally checks turn-scoped Codex RPCs across failed-turn
resume and live interruption. Its 101-reasoning-item fixture forces pagination and compares the
actual next model request, while an unrelated older turn must not be fetched. Unit tests cover
cross-turn ownership, mode and cursor failures, compaction barriers, and stdout/scheduler ordering.

## Standing instructions across a resume

The runner supplies the Session's stored standing instructions at every native start or cold resume,
so they remain model-visible after later compaction. Reopening a Session still requires the same
spec; the runner does not expose instruction editing.

- **Claude Code:** `appendSystemPrompt` in each `initialize` request.
- **Codex:** `developerInstructions` in both `thread/start` and `thread/resume`. Resume initially
  replays the stored developer message, but compaction regenerates that message from the resumed
  configuration. Replaying the old message on the first resumed turn does not establish that the
  configuration needed by a later compaction was restored.

The Runner integration test covers automatic compaction in the same process, after a Runner
restart, and after quiesce/resume under a new incarnation. The Sandbox suspend/resume system test
also checks the continuation request after compaction following actual Pod replacement.

## What the harnesses do not promise

- Claude Code serializes transcript appends, but enqueue acceptance is not a durability fence.
  Explicit flush, terminal result, and orderly shutdown fence pending writes; a harness killed
  outright can lose the conversation since its last completed fence. The runner stops harnesses by
  closing stdin, and the runner's own termination does the same for every session, so the pod's
  SIGTERM path gives Claude an orderly flush opportunity but does not turn an earlier
  `CommandAdmitted` into proof of native persistence. See
  the [native source findings](../native/docs/claude_source_findings.md#transcript-writer-static-inspection-of-21252).
- Codex reports no aggregated output for a shell command that outlived its first read, and keeps a
  streamed model connection open after an interrupt; neither changes the events above.
- Native approval prompts, user dialogs, and hook callbacks never block a turn. Claude Code runs
  with explicit `--permission-mode bypassPermissions`, so its automatic classifier does not
  decide tool permissions. The runner answers every `can_use_tool` request allow;
  `hook_callback` and any unrecognised control request get an
  error answer. Codex runs under `approval_policy: never`, so no approval request arrives; a server
  request that does (approval, user input, elicitation) is refused with a JSON-RPC error.

## Harness-originated messages

Native subagents are enabled but are not Agentplane Sessions. Their received frames stay in the
root Session's raw log; they do not create normalized items, start or complete its turns, or
acknowledge its pending inputs. Codex translated notifications must carry the root thread ID.
Claude frames with `parent_tool_use_id` and `task_*` system notices are raw-only. Native control
requests and RPC responses still get their normal handling. Root delegation items and results
are normalized, including Codex `agentsStates`. A Codex v2 `subAgentActivity` tool result reports
that the activity was observed, not child success; unknown activity kinds remain raw-only.
There is no child discovery, attachment, or independent control in the Session protocol.

A harness says things of its own: a hook's feedback, a compaction boundary, a status or warning
notice, the `<system-reminder>` context Claude Code adds to a turn. Every one of them reaches the
log as a `Native` event, verbatim — that is what "delivered in both directions" above means. None
of them is derived into an item, so a client reading only the item events does not see them, and
what a client that reads `Native` has to work with differs by harness:

- **Claude Code.** `system` frames (`compact_boundary`, `notification`, `informational`) and any
  frame outside the wire union parse and then fall through the adapter's dispatch. `<system-reminder>`
  blocks have only ever been observed in the request the harness sends upstream, which the
  recording proxy sees and the runner does not; whether the harness also emits them on stdout is
  unknown. Hook events need `--include-hook-events`, which the runner does not pass, and a
  `hook_callback` control request is refused so the turn cannot block on it.
- **Codex.** Notifications (`thread/compacted`, `hook/started`, `hook/completed`, `warning`,
  `deprecationNotice`) fall through dispatch the same way. Item-shaped ones do not: `contextCompaction`
  and `hookPrompt` reach `UnknownItem` and are emitted as **tool calls** named after the item type,
  so they already appear in a conversation view, mislabelled.

The hook wire surface above is observed live only by the capture probe's `hooks` and `hooks_deny`
scenarios ([`../docs/hooks.md`](../docs/hooks.md)); the scripted tests and the runner run with
hooks off, so the runner's own handling of it is read off the harnesses' schemas.

## Not covered yet

- Log compaction or retention; a session log grows for the session's lifetime.
- Determining whether an incomplete tool call caused side effects outside the harness process group.
- Duplicate-free recovery when native execution precedes durable runner evidence.

### History-independent recovery and replay

- Session recovery reads a durable checkpoint, not all historical events. The checkpoint and
  event publication boundary commit atomically with each event and command outcome.
- Each ingress push reads bounded pages from durable storage. A slow ingress does not accumulate
  a private copy of the intervening history in runner memory.
- Historical command identities and debug checkpoints remain queryable in durable storage;
  in-memory scheduling tracks outstanding work and releases terminal commands.
- These bounds exclude the native harness process and the size of individual events and
  outstanding work. Native harness resume may still read its own history.
