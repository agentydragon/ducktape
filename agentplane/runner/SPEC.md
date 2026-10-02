# Runner protocol

One gRPC contract over the native Claude Code and Codex harnesses. A caller drives a session
through it without knowing which harness is behind it: the only harness-selection field it ever
sets is `SessionSpec.harness`. The wire definition is [`protocol.proto`](protocol.proto); this
page is what the runner guarantees about it.

The runner does not own product Threads, app command persistence, Kubernetes lifecycle, or the
Thread-page projection. Their cross-layer contract is [Thread, runner, and harness layering](../docs/thread_layering.md).

## Initialization

- `Initialize` receives integration-app configured shell source and executes it with `/bin/sh -eu`
  from `state_dir` before the Sandbox's workload starts.
- The first request durably selects the script digest at the fixed path
  `<state_dir>/initialization/request.json`, before the script starts. The runner thereafter refuses
  changed source, including after a failed attempt: two possibly incompatible initializations must
  never be combined in one Sandbox. An exact request after success only replays the original record;
  the exact script may be retried after failure.
- Stdout, stderr, and each attempt's terminal result are appended to a persistent event log with
  dense Sandbox-scoped sequence numbers. `InitializeRequest.after_sequence` is a reconnect cursor:
  the runner first replays later saved events, then follows live output through the current attempt.
  Disconnecting a client does not stop the script or recording, and a future exact request can
  replay the complete output after the runner restarts.
- The runner accepts source only over this in-cluster control protocol; how the integration app
  selects that source is outside the runner contract.

## Sessions

- A session is one native conversation. Its id is client-chosen, up to 128 characters of
  `[A-Za-z0-9._-]` starting alphanumeric, and the runner keeps everything about it under
  `<state_dir>/sessions/<session_id>/`: the harness's own persistence and the runner's session log.
- `Open` with an unknown id creates the session from `spec`; with a known id it attaches, and a
  supplied `spec` must equal the stored one. Open with a spec starts the harness when it is not running,
  resuming the native conversation when the session has one, and creates `spec.cwd`, which must
  be absolute, when it does not exist yet. A harness that does not survive its launch and
  handshake ends the stream with an error naming its exit status and the end of its stderr.
- An unknown session may select one setup script on `Open`. The runner creates `spec.cwd`, executes
  the script with `/bin/sh -eu` from that directory, and records started, stdout/stderr, and terminal
  events in the session's durable Event log. `Open` does not wait for setup to complete. The harness
  starts only after setup exits successfully. A failed or interrupted setup is terminal for that
  session: later `Open` calls replay its history without rerunning the script or starting the
  harness. A different setup source is refused. Start a new session to try again.
- `Open` without a spec observes an existing session without starting its harness. A stopped
  session replays its log and ends the stream; resuming requires an explicit spec.
- `spec.instructions` are the session's standing instructions: what the session is for, and the
  orders that hold for every turn of it. They reach the model appended to the harness's own system
  prompt, so each harness keeps its coding-agent policy; empty is a session without any. They are
  fixed for the session's life, because a `spec` supplied on re-attach must equal the stored one.
- The deployment may supply `AGENTPLANE_MODEL_CONTEXT_WINDOWS` as a JSON map from model ids to
  positive token counts. A listed route sets Claude's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` or Codex's
  `model_context_window` when its harness starts, including a resume. Codex derives its default
  automatic-compaction threshold from the overridden window. Unlisted models retain their normal
  harness metadata. This map is runner configuration, not session protocol data or child environment.
- A session survives the runner process. A runner that starts on a state directory loads every
  session in it; what the previous runner had running is reported as lost (below).
- `ListSessions` returns every session in the state directory with its spec, harness state,
  setup state, active turn, and last cursor, so a client that keeps no record of its own finds them again.

## Attachments

- One `Attach` stream is one attachment. The first client message is `Open`; the first server
  message is `Attached`, carrying the spec, setup and harness states, `last_cursor`, the log position at
  attach time, and the active turn id as of that position.
- The runner then replays every `EventEntry` with a cursor greater than `Open.follow.after_cursor`,
  in order, and continues with live entries. A client that passes the last cursor it processed sees
  neither a gap nor a duplicate; a cursor beyond `last_cursor` ends the stream with an error.
- Multiple attachments independently replay and follow the session. Each may issue client-chosen,
  idempotent `Command`s; the runner serializes them with the session lock and deduplicates by
  `command_id`.
- `Detach`, or a dropped connection, ends the stream and nothing else. The harness keeps running
  and its events keep accruing in the log.
- `StopRunnerSession` interrupts an active turn, stops the harness, reports `HarnessExited`, and
  ends the streams after every observer drains the terminal events. The session stays resumable.

## Events

Every observation is delivered in a common `EventEntry`: the runner's session log assigns a dense
cursor from 1 and a stable source id plus source sequence in its `origin`. Cursors order a
particular serving log; an importer may assign a different cursor without changing the origin or
event. The event has a timestamp. Derived events name the `Native` events they came from in
`source_sequences`; the harness frames themselves are delivered verbatim, in both directions, so
harness-native detail is one lookup away.

| Family  | Events                                                                                                                                       |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| harness | `HarnessStarted` (resumed, pid), `HarnessExited` (exit code, stopped by the runner), `HarnessLost`, `HarnessStderr`                          |
| command | `CommandAdmitted`, `CommandFailed`, `CommandNoop`, `HarnessUserMessageConfirmed`, `ModelChanged`                                             |
| turn    | `TurnStarted`, `TurnCompleted` (`COMPLETED`, `INTERRUPTED`, `FAILED`, `PROCESS_LOST`)                                                        |
| item    | `ItemStarted` (assistant text, reasoning, tool call), `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`, `ToolOutputDelta`, `ItemCompleted` |
| native  | `Native` (direction, exact line)                                                                                                             |

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
  it reconciles nonterminal commands with their original ids and replays committed Events unchanged. Native execution before
  durable effect evidence is not yet proven duplicate-free across restart.
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
- A model change is rejected with a new-session diagnostic when the current and requested routes
  have different configured context windows, including a transition between a configured and
  unlisted route. Same-window changes and changes between unlisted models keep their existing
  behavior. This keeps the per-process context and compaction limit aligned with the route for the
  lifetime of its native thread; start a new session to use a different window.
- `StopRunnerSession` is terminal as `HarnessExited`, naming its command id. A stopped harness is
  instead a `CommandNoop`.

## Durability and restart

- One runner has exclusive lifetime ownership of one retained `state_dir`. It takes a nonblocking
  POSIX exclusive lock on the fixed `<state_dir>/.agentplane-runner-owner` inode before it reads
  sessions, opens a journal, listens, runs initialization, or starts a harness. A contender that
  cannot take that lock exits without serving the directory. The normal runner shutdown releases
  ownership only after its sessions stop and their journals close.
- The owner descriptor crosses the runner/harness boundary through a native-process supervisor.
  When the runner dies, its harness-input writer disappears and the supervisor receives
  parent-death notification. It sends `SIGTERM` to the native harness process group, gives the
  leader five seconds to preserve native resume state, and then force-stops the group. If the
  leader exits first but a tool remains, it force-stops that remainder immediately, including
  when the harness exits independently while its runner stays alive. The supervisor
  and any child that still retains the inherited descriptor hold ownership until they exit, so a
  replacement cannot append Events or dispatch native work in that interval. This fences the
  native harness process group, including a normal tool child; a process that deliberately escapes
  that group and closes inherited descriptors is outside the runner's containment contract. The
  supported state mount is the checked-in node-local `local-path-ovh-hdd` PVC. A remote or otherwise
  unreliable locking filesystem is unsupported rather than a substitute cross-host fencing authority.
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
- A runner that finds a session it had running reports `HarnessLost`, then `TurnCompleted` with
  `PROCESS_LOST` if a turn was active. It replays committed Events unchanged and
  reconciles the remaining commands after the next explicit `Open` starts the
  harness. There is no indeterminate command outcome.
- A runner that finds a session whose setup had started without a terminal event records
  `SetupInterrupted`. It never executes that script again. The inherited state-owner descriptor
  keeps a replacement runner out until a surviving setup child exits.
- A harness that exits on its own is reported the same way, as `HarnessExited` with the exit code
  instead of `HarnessLost`.
- A runner that receives SIGTERM stops every running harness through the stop ladder (stdin
  close, then SIGTERM, then SIGKILL, five seconds per step) without interrupting an active turn
  first, records `HarnessExited` with `stopped_by_runner`, then stops its server with a five-second
  grace and exits. Whatever supervises the runner must allow it at least twenty seconds before
  killing it; a harness killed outright is `HarnessLost` on the next start instead.

## Interrupted turns and continuation recovery

The runner reports `ConversationReconciled` after an ordinary interruption and after native
resume, before delivering subsequent input. It covers the most recent completed turn and any
later interrupted turns on resume; an ordinary interruption covers that turn. Each previously
observed item is `RETAINED`, `ABSENT`, `REVISED` (with its continuing content), or `UNKNOWN`
(with a reason). These are point-in-time statements about continuation, not guarantees against
future compaction. Native interpretation belongs to the runner adapter; clients consume the
same dispositions for every harness.

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

## Standing instructions across a resume

Both harnesses put the session's instructions in front of the model on every turn, a resumed
conversation included, but by different routes, and the difference decides what a client could ever
do with a changed value.

- **Claude Code** takes them as `appendSystemPrompt` in the `initialize` control request, which the
  runner sends at _every_ harness start. The text lands in the system prompt block after the
  harness's own prompt, separated by a blank line, and the value in the spec is what each launch
  sends. A different value would take effect at the next start.
- **Codex** takes them as `developerInstructions` on `thread/start`, which the runner sends only for
  a _fresh_ thread. The app-server stores them as a `developer` message at the head of the thread's
  history, and a resumed thread replays that message out of its rollout, so they survive a resume
  the runner never restates. What a resume cannot do is change them, and the wire looks like it can:
  `thread/resume` takes a `developerInstructions` override, the app-server accepts it without an
  error or a warning, and the model still sees only the message the thread was started with — never
  the new text, and never both. The measurement is the resume row of the
  [roster](../native/docs/protocol_roster.md).

So the protocol's "instructions are fixed for the session's life" is not a stylistic choice: it is
the strongest promise both harnesses can keep. Editing a live session's standing instructions is
buildable on Claude Code and is not buildable on Codex without starting a new thread, which is a new
session and a new transcript.

## What the harnesses do not promise

- Claude Code serializes transcript appends, but enqueue acceptance is not a durability fence.
  Explicit flush, terminal result, and orderly shutdown fence pending writes; a harness killed
  outright can lose the conversation since its last completed fence. The runner stops harnesses by
  closing stdin, and the runner's own termination does the same for every session, so the pod's
  SIGTERM path gives Claude an orderly flush opportunity but does not turn an earlier
  `CommandAdmitted` into proof of native persistence. See
  [`../docs/claude_runtime_contracts.md`](../docs/claude_runtime_contracts.md).
- Codex reports no aggregated output for a shell command that outlived its first read, and keeps a
  streamed model connection open after an interrupt; neither changes the events above.
- Native approval prompts, user dialogs, and hook callbacks never block a turn. Claude Code's
  `can_use_tool` is answered allow; `hook_callback` and any unrecognised control request get an
  error answer. Codex runs under `approval_policy: never`, so no approval request arrives; a server
  request that does (approval, user input, elicitation) is refused with a JSON-RPC error.

## Harness-originated messages

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

- Read-only authorization for follower attachments; every attachment may issue commands.
- Log compaction or retention; a session log grows for the session's lifetime.
- Transport security; the listener is plaintext on loopback.
- Determining whether an incomplete tool call caused side effects outside the harness process group.
- Duplicate-free recovery when native execution precedes durable runner evidence.

### History-independent recovery and replay

- Session recovery reads a durable checkpoint, not all historical events. The checkpoint and
  event publication boundary commit atomically with each event and command outcome.
- Each attachment reads bounded pages from durable storage. Slow readers do not accumulate
  a private copy of the intervening history in runner memory.
- Historical command identities and debug checkpoints remain queryable in durable storage;
  in-memory scheduling tracks outstanding work and releases terminal commands.
- These bounds exclude the native harness process and the size of individual events and
  outstanding work. Native harness resume may still read its own history.
