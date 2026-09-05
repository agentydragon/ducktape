# Runner protocol

One gRPC contract over the native Claude Code and Codex harnesses. A caller drives a session
through it without knowing which harness is behind it: the only provider-specific field it ever
sets is `SessionSpec.provider`. The wire definition is [`protocol.proto`](protocol.proto); this
page is what the runner guarantees about it.

## Sessions

- A session is one native conversation. Its id is client-chosen, up to 128 characters of
  `[A-Za-z0-9._-]` starting alphanumeric, and the runner keeps everything about it under
  `<state_dir>/sessions/<session_id>/`: the harness's own persistence and the runner's session log.
- `Open` with an unknown id creates the session from `spec`; with a known id it attaches, and a
  supplied `spec` must equal the stored one. Open starts the harness when it is not running,
  resuming the native conversation when the session has one, and creates `spec.cwd`, which must
  be absolute, when it does not exist yet.
- `spec.instructions` are the session's standing instructions: what the session is for, and the
  orders that hold for every turn of it. They reach the model appended to the harness's own system
  prompt, so each harness keeps its coding-agent policy; empty is a session without any. They are
  fixed for the session's life, because a `spec` supplied on re-attach must equal the stored one.
- A session survives the runner process. A runner that starts on a state directory loads every
  session in it; what the previous runner had running is reported as lost (below).
- `ListSessions` returns every session in the state directory with its spec, harness state,
  active turn, and last sequence, so a client that keeps no record of its own finds them again.

## Attachments

- One `Attach` stream is one attachment. The first client message is `Open`; the first server
  message is `Attached`, carrying the spec, the harness state, the active turn id, and
  `last_sequence`, the log position at attach time.
- The runner then replays every event with a sequence greater than `Open.after_sequence`, in
  order, and continues with live events. A client that passes the last sequence it processed sees
  neither a gap nor a duplicate; a cursor beyond `last_sequence` ends the stream with an error.
- One attachment controls a session at a time. A newer `Open` supersedes the current one, whose
  stream ends with an error.
- `Detach`, or a dropped connection, ends the stream and nothing else. The harness keeps running
  and its events keep accruing in the log.
- `Shutdown` interrupts an active turn, stops the harness, reports `HarnessExited`, and ends the
  stream. The session stays resumable.

## Events

Every event carries a session-scoped `sequence`, dense from 1 and strictly increasing across
attachments and runner restarts, and a timestamp. Derived events name the `Native` events they
came from in `source_sequences`; the harness frames themselves are delivered verbatim, in both
directions, so provider detail is one lookup away.

| Family  | Events                                                                                                                                       |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| harness | `HarnessStarted` (resumed, pid), `HarnessExited` (exit code, stopped by the runner), `HarnessLost`, `HarnessStderr`                          |
| input   | `InputSubmitted`, `InputAccepted` (turn id), `InputRejected` (reason), `InputUncertain`                                                      |
| turn    | `TurnStarted`, `TurnCompleted` (`COMPLETED`, `INTERRUPTED`, `FAILED`, `PROCESS_LOST`)                                                        |
| item    | `ItemStarted` (assistant text, reasoning, tool call), `TextDelta`, `ToolArgumentsDelta`, `ToolArguments`, `ToolOutputDelta`, `ItemCompleted` |
| native  | `Native` (direction, exact line)                                                                                                             |

Items are the units of assistant output within a turn. Text and reasoning items stream `TextDelta`
and complete with their full text; tool calls stream their arguments where the harness does,
report the complete `ToolArguments`, stream output where the harness does, and complete with the
harness's outcome. Tool names and argument shapes are the harness's own.

## Inputs

- `input_id` is client-chosen and idempotent. An input is reported as `InputSubmitted`, carrying
  its text, when the runner takes it, then `InputAccepted` once the harness acknowledges native
  admission, or `InputRejected`. For Claude, this acknowledgement is `command_lifecycle: queued`:
  it proves command-queue admission, not that the input has started, entered the native transcript,
  reached the model, or become durable. The exact provider boundary remains visible in the source
  `Native` event.
- An input while no turn is active starts a turn: `TurnStarted` precedes its `InputAccepted`. An
  input while a turn is active joins that turn; the harness decides where in the model's context
  it lands.
- Resending an id the log already settled delivers nothing to the harness; the runner re-emits the
  original outcome so a client that retried after a lost connection learns it. Resending an id
  that is still unsettled changes nothing.
- `Interrupt` ends the active turn, which completes as `INTERRUPTED`; with no active turn it is
  ignored.

## Durability and restart

- The log is written before an event is delivered. Harness, input, and turn lifecycle events are
  synced to disk; deltas and native evidence are flushed but not synced, so a crash can shorten
  their tail but never reorder or lose a decision.
- A runner that finds a session it had running reports `HarnessLost`, then `TurnCompleted` with
  `PROCESS_LOST` if a turn was active, then `InputUncertain` for each input submitted but never
  settled. The next `Open` resumes the native conversation and reports `HarnessStarted` with
  `resumed`.
- `InputUncertain` is the one window the runner cannot close: the input may or may not be in the
  harness's transcript. Resending it is the client's decision.
- A harness that exits on its own is reported the same way, as `HarnessExited` with the exit code
  instead of `HarnessLost`.
- A runner that receives SIGTERM stops every running harness through the stop ladder (stdin
  close, then SIGTERM, then SIGKILL, five seconds per step) without interrupting an active turn
  first, records `HarnessExited` with `stopped_by_runner`, then stops its server with a five-second
  grace and exits. Whatever supervises the runner must allow it at least twenty seconds before
  killing it; a harness killed outright is `HarnessLost` on the next start instead.

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
  `InputAccepted` into proof of native persistence. See
  [`../docs/claude_runtime_contracts.md`](../docs/claude_runtime_contracts.md).
- Codex reports no aggregated output for a shell command that outlived its first read, and keeps a
  streamed model connection open after an interrupt; neither changes the events above.
- Native approval prompts, user dialogs, and hook callbacks are refused with an error answer, so a
  turn never blocks on them. Both harnesses run with approvals off inside the sandbox.

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

Neither harness has been run with hooks registered, so none of the hook wire surface above is
observed rather than read off the harnesses' own schemas.

## Not covered yet

- Read-only follower attachments; only one attachment per session.
- Log compaction or retention; a session log grows for the session's lifetime.
- Transport security; the listener is plaintext on loopback.
- Recovery semantics for a turn lost mid-tool beyond reporting `PROCESS_LOST`.
