# Harness Host Session protocol

Harness Host owns native Session state, adapters, durable records and journals. This document
records the current Session behavior and its producer side of History ingress. Environment identity,
bootstrap, public control observation and lifecycle aggregation belong to Environment Runtime.

The shared Session messages are in [`session.proto`](../../../../proto/session.proto); the current
public control RPCs are in [Environment Runtime control](../../control/environment_runtime.proto).

## Sessions

- A session is one native conversation. Its id is client-chosen, up to 128 characters of
  `[A-Za-z0-9._-]` starting alphanumeric, and the Harness Host keeps its session log under
  `<state_dir>/sessions/<session_id>/`, with Claude's own persistence for a Claude Session. Codex
  persists every Codex Session of the Harness Host in one Codex home, `<state_dir>/codex/`.
- Each Claude Session runs its own harness process. Every Codex Session is one thread of the
  Harness Host's one Codex app-server, started when a Codex Session first needs a harness. Its command
  line and Codex home apply to every Codex Session; each thread carries its own Session's working
  directory, instructions, model, reasoning effort and tool environment, including the notification
  destination. A native frame naming a thread is recorded only in that thread's Session, a
  sub-agent's in its root's; a frame naming none, and the app-server's stderr, is recorded in every
  Codex Session the app-server hosts. Interrupting one Session's turn leaves the others running. An
  app-server exit ends every hosted Session's harness: each records `HarnessExited` and, with an
  active turn, `PROCESS_LOST`, and resumes its thread when it next needs a harness.
- Native Codex plugins, app connectors, lifecycle hooks and skill-triggered MCP dependency
  installation are disabled. Native Codex product analytics, feedback submission and startup update
  checks are disabled.
- `OpenSession` with an unknown id durably reserves the session from `spec`; `spec.cwd` must be
  absolute. With a known id it confirms the reservation: the `spec` must equal the stored one.
  Opening never starts a harness. Later admitted execution work starts it in the background, and the
  Harness Host returns that command's admission without waiting for startup. A harness that does not
  survive its launch and handshake produces durable launch-failure and command-failure events.
- An unknown session may select one setup script on `OpenSession`. The source is retained with the
  session record so an identical retry can resume a first reservation that failed before
  publication. The Harness Host creates `spec.cwd`, executes the script with `/bin/sh -eu` from that
  directory after bootstrap succeeds, and records started, stdout/stderr, and terminal events in
  the session's durable Event log. `OpenSession` does not wait for setup. The harness starts only
  after setup exits successfully and a command is admitted. After a restart, a setup that was still
  running is recorded as interrupted before the Session is published; its retained source is never
  replayed. A failed or interrupted setup is terminal for that session and is never rerun. A
  different setup source is refused. Start a new session to try again.
- `spec.instructions` are the session's standing instructions: what the session is for, and the
  orders that hold for every turn of it. They reach the model appended to the harness's own system
  prompt, so each harness keeps its coding-agent policy. A caller's empty task leaves the platform part. They are
  fixed for the session's life. The Harness Host composes them from the deployment's platform instructions
  (`environment-runtime.json`’s `platform_instructions`), the Session's
  notification destination (the Sandbox identity from the launch file and the Session ID) and the
  caller's task, in that order; callers supply only the task. They are fixed because a `spec` supplied on reopening must equal the stored one.
  Platform guidance supplies deployment service addresses and essential constraints; detailed
  workflows are discoverable through the independent services' `/llms.txt` indexes.
- For a Claude Session the Harness Host asks the workload-authenticated LLM ingress for `ModelConfig` when a
  session or model selection is made. Its `total_context_budget_tokens` is a client
  budget shared by input and output, not a provider maximum or an output request cap.
  The Harness Host sets Claude's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from it; routes without an override use
  the harness's normal metadata. The resolved value is retained in the Harness Host's session record so a
  resume uses the same compaction window. Claude model changes across different resolved windows
  require a new session.
- Codex takes no budget from the Harness Host: its provider names the ingress model catalog
  (`/agentplane/codex/models` on the model endpoint's origin), and each turn uses the entry for the
  turn's model slug, so a model change across windows is allowed and an edited catalog reaches a
  running Codex before its next turn. A route without an entry gets Codex's metadata for an unknown model.
- A session survives the Harness Host process. A Harness Host that starts on a state directory loads every
  session in it; what the previous Harness Host had running is reported as lost (below).
- `ListSessions` returns every session in the state directory with its spec, harness state,
  setup state, active turn, and last cursor, so a client that keeps no record of its own finds them again.

## Event ingress

- The Harness Host pushes every session's journal to the
  [ingress](../../../../docs/runner_control.md#event-ingress) at `ingress.target`, one `Push` call per session until the ingress
  acknowledges the session's seal: at startup, when a session is created, and after a call is lost.
  Each call announces the session's `Attached` snapshot and sends the journal's entries after the
  ingress's first acknowledgement, replayed and then live, at most 1024 beyond the latest
  acknowledgement, then `Sealed` once the journal is sealed.
- The Harness Host records each session's latest acknowledgement on its state volume and deletes no
  journal entry on it, then publishes the acknowledgement to Harness Host lifecycle observation. Journal
  read/wait, acknowledgement persistence, and lifecycle-observation failures are local and fail the
  Harness Host; remote stream loss retries with the control stream's backoff. A call the ingress refuses as
  another writer's, or whose first acknowledgement is beyond the journal, retries at the maximum.
  These remote failures do not hold up another session, a command or a lifecycle request: the journal is the buffer.
- The ingress bearer is a placeholder the egress proxy substitutes for the ingress's own audience;
  it is independent of Environment Runtime control and never authenticates to the Gateway.

## Events

Every observation is delivered in a common `EventEntry`: the Harness Host's session log assigns a dense
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
| harness | `HarnessStarted` (resumed, pid), `HarnessExited` (exit code, stopped by the Harness Host), `HarnessLost`, `HarnessStderr`                    |
| command | `CommandAdmitted`, `CommandFailed`, `CommandNoop`, `HarnessUserMessageConfirmed`, `ModelChanged`                                             |
| turn    | `TurnStarted`, `TurnCompleted` (`COMPLETED`, `INTERRUPTED`, `FAILED`, `PROCESS_LOST`)                                                        |
| item    | `ItemStarted` (assistant text, reasoning, tool call), `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`, `ToolOutputDelta`, `ItemCompleted` |
| native  | `Native` (direction, exact line)                                                                                                             |

Connection-loss behavior pinned by `native/{claude,codex}/test_connection_loss.py` and
[`test_turns.py`](../../../e2e/test_turns.py): with the Harness Host driving the
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

- A `Command` is client-chosen and idempotent. The Harness Host commits the full command and its
  `CommandAdmitted` Event together. Admission means the Harness Host owns trying to process that exact command;
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
  and Claude emits neither a user echo nor an input UUID: the Harness Host reports the tool-result id plus
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

- Runtime holds `<state_dir>/.agentplane-environment-runtime-owner` throughout the environment
  lifetime. Host separately locks `<state_dir>/harness-host/.agentplane-harness-host-owner` before
  opening Session records and journals. These new paths are a direct cutover; old workloads must be
  stopped before the new binaries reuse their state volumes.
  Runtime alone owns `<state_dir>/sessions/sealed`; Host never clears or adopts that marker.
  Host releases its lock only after native cleanup and journal close. Runtime releases the
  environment lock only after its child domains are fenced. Durable Session IDs and paths do not move.
- Production Pods explicitly select PID1 supervision and reject Runtime startup outside container
  PID 1. Embedded Host shares Runtime's PID1 supervisor for native helpers. Normal Host
  `native_stopped` reports prove its native cleanup; abnormal Runtime exit alone does not prove that
  escaped process groups are gone.
  One reaper owns direct-child exit statuses and adopted orphans; native leaders never compete
  with an `exec.Cmd.Wait` goroutine. Leader exit kills its remaining group; completion waits until
  the group disappears. Stop sends TERM, then KILL after its grace budget. Non-PID1 fixtures
  explicitly select the internal subprocess supervisor, whose parent-liveness pipe and trusted
  owner descriptor fence the native group after Harness Host death.
  Both Pod backends require observed container-runtime exit to fence escaped process groups and
  natives that close inherited descriptors. A lock acquisition or force-deleted Pod's API
  disappearance does not prove that outer fence. Replacement after node loss or force deletion
  requires a prior-container or external node/storage fence.
  [Supervision](../../supervision/docs/supervision.md#pid-1-and-the-outer-fence) describes these boundaries.
- In VMs every native launch is a systemd transient `Type=exec` service, bound to the Runtime unit,
  with `Restart=no` and five-second stop grace. A condition requires the owning service invocation's
  marker in its systemd-managed runtime directory; delayed requests from a previous invocation
  cannot execute after replacement. Systemd contains descendants across process-group changes.
  Harness Host reports completion only after the service is inactive/failed and its execution
  domain is empty. Native services do not inherit the state-owner descriptor: a replacement may
  acquire the lock after Harness Host death, but it must discover and fence the prior units in its
  explicit Sandbox/state owner scope before opening Session stores, journals or admitting native
  work. It verifies each unit's expected slice and leaves unrelated units alone. A lost or
  canceled launch reply is resolved by fencing that same launch identity, never by issuing a
  second launch. A status-read timeout on a connected manager retries without declaring native
  exit or releasing ownership. Manager disconnection remains a fencing barrier, preventing
  successors and seals until the native domain is proven empty.
  [Supervision](../../supervision/docs/supervision.md) describes the resource hierarchy and backend boundary.
- The supported state mount is the checked-in node-local `piraeus-hdd-local` PVC. A remote or
  otherwise unreliable locking filesystem is unsupported rather than a substitute cross-host
  fencing authority.
- A command admission and its public Event commit atomically. A terminal outcome and its public
  Event also commit atomically, including every origin of a coalesced user message. Recovery
  retains their exact payloads and association; it does not reconstruct missing receipt Events.
- Every public Event, including native frames and streaming deltas, crosses the Harness Host storage
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
- A Harness Host that finds a session it had running reports `HarnessLost`, then `TurnCompleted` with
  `PROCESS_LOST` if a turn was active. It replays committed Events unchanged and automatically
  starts the harness in the background to reconcile remaining admitted commands. A dispatched command
  with no terminal evidence remains pending; a missing response is not a failure or proof that retry
  is safe. Command dispatch returns after the native write; correlated response Events advance the
  command state and release later response-dependent commands. If a response was committed before the
  restart, recovery translates it and settles the command. A planned interruption without terminal
  evidence also remains pending because the Harness Host cannot know whether it reached the harness.
  Unanswered Codex requests recovered from the journal do not block fresh input; their commands
  remain pending and are never resent. Newly sent requests gate later input until their responses
  are recorded.
- A Harness Host that finds a session whose setup had started without a terminal event records
  `SetupInterrupted`. It never executes that script again. The Pod container-runtime fence or VM startup fencing barrier prevents a replacement
  from opening journals or running work while a surviving setup child remains.
- A harness that exits on its own is reported the same way, as `HarnessExited` with the exit code
  instead of `HarnessLost`.
- A Harness Host that receives SIGTERM stops every running harness without interrupting an active turn
  first. Pods close stdin, then use SIGTERM and SIGKILL with five seconds per step. VMs close stdin
  and request systemd stop, with five seconds after SIGTERM before SIGKILL. Harness Host records `HarnessExited` with `stopped_by_runner`, then exits. Whatever supervises the
  Harness Host must allow it at least twenty seconds before killing it; a harness killed outright is `HarnessLost` on the next start instead.

## Interrupted turns and continuation recovery

The Harness Host reports `ConversationReconciled` after an ordinary interruption and after native
resume, before delivering subsequent input. It covers the most recent completed turn and any
later interrupted turns on resume; an ordinary interruption covers that turn. Each previously
observed item is `RETAINED`, `ABSENT`, `REVISED` (with its continuing content), or `UNKNOWN`
(with a reason). These are point-in-time statements about continuation, not guarantees against
future compaction. Native interpretation belongs to the Harness Host adapter; clients consume the
same dispositions for every harness.

Codex reconciliation requires `paginated` history, checked from `thread/start` or `thread/resume`.
It pages `thread/items/list` with a native `turnId`, at most 100 items per response. An ordinary
interruption reads that turn; resume reads the most recent completed turn and later turns from the
Harness Host journal. An item that finishes in a later turn adds its original owning turn to the reads.
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

`//agentplane/env/e2e:test_restart` additionally checks turn-scoped Codex RPCs across failed-turn
resume and live interruption. Its 101-reasoning-item fixture forces pagination and compares the
actual next model request, while an unrelated older turn must not be fetched. Unit tests cover
cross-turn ownership, mode and cursor failures, compaction barriers, and stdout/scheduler ordering.

## Standing instructions across a resume

The Harness Host supplies the Session's stored standing instructions at every native start or cold resume,
so they remain model-visible after later compaction. Reopening a Session still requires the same
spec; the Harness Host does not expose instruction editing.

- **Claude Code:** `appendSystemPrompt` in each `initialize` request.
- **Codex:** `developerInstructions` in both `thread/start` and `thread/resume`. Resume initially
  replays the stored developer message, but compaction regenerates that message from the resumed
  configuration. Replaying the old message on the first resumed turn does not establish that the
  configuration needed by a later compaction was restored.

The Harness Host integration test covers automatic compaction in the same process, after a Harness Host
restart, and after quiesce/resume under a new incarnation. The Sandbox suspend/resume system test
also checks the continuation request after compaction following actual Pod replacement.

## What the harnesses do not promise

- Claude Code serializes transcript appends, but enqueue acceptance is not a durability fence.
  Explicit flush, terminal result, and orderly shutdown fence pending writes; a harness killed
  outright can lose the conversation since its last completed fence. The Harness Host stops harnesses by
  closing stdin, and the Harness Host's own termination does the same for every session, so the pod's
  SIGTERM path gives Claude an orderly flush opportunity but does not turn an earlier
  `CommandAdmitted` into proof of native persistence. See
  the [native source findings](../../../../native/docs/claude_source_findings.md#transcript-writer-static-inspection-of-21252).
- Codex reports no aggregated output for a shell command that outlived its first read, and keeps a
  streamed model connection open after an interrupt; neither changes the events above.
- Native approval prompts, user dialogs, and hook callbacks never block a turn. Claude Code runs
  with explicit `--permission-mode bypassPermissions`, so its automatic classifier does not
  decide tool permissions. The Harness Host answers every `can_use_tool` request allow;
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
  recording proxy sees and the Harness Host does not; whether the harness also emits them on stdout is
  unknown. Hook events need `--include-hook-events`, which the Harness Host does not pass, and a
  `hook_callback` control request is refused so the turn cannot block on it.
- **Codex.** Notifications (`thread/compacted`, `hook/started`, `hook/completed`, `warning`,
  `deprecationNotice`) fall through dispatch the same way. Item-shaped ones do not: `contextCompaction`
  and `hookPrompt` reach `UnknownItem` and are emitted as **tool calls** named after the item type,
  so they already appear in a conversation view, mislabelled.

The hook wire surface above is observed live only by the capture probe's `hooks` and `hooks_deny`
scenarios ([hooks](../../../../docs/hooks.md)); the scripted tests and the Harness Host run with
hooks off, so the Harness Host's own handling of it is read off the harnesses' schemas.

## Not covered yet

- Log compaction or retention; a session log grows for the session's lifetime.
- Determining whether an incomplete tool call caused side effects outside the harness process group.
- Duplicate-free recovery when native execution precedes durable Harness Host evidence.

### History-independent recovery and replay

- Session recovery reads a durable checkpoint, not all historical events. The checkpoint and
  event publication boundary commit atomically with each event and command outcome.
- Each ingress push reads bounded pages from durable storage. A slow ingress does not accumulate
  a private copy of the intervening history in Harness Host memory.
- Historical command identities and debug checkpoints remain queryable in durable storage;
  in-memory scheduling tracks outstanding work and releases terminal commands.
- These bounds exclude the native harness process and the size of individual events and
  outstanding work. Native harness resume may still read its own history.
