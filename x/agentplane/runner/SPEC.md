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

- `input_id` is client-chosen and idempotent. An input is reported as `InputSubmitted` when the
  runner takes it, then `InputAccepted` once the harness has taken it into its transcript, or
  `InputRejected`.
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

## What the harnesses do not promise

- Claude Code writes its transcript when it exits cleanly; a harness killed outright can lose the
  conversation since its last write. The runner stops harnesses by closing stdin, and the runner's
  own termination does the same for every session, so the pod's SIGTERM path preserves transcripts.
- Codex reports no aggregated output for a shell command that outlived its first read, and keeps a
  streamed model connection open after an interrupt; neither changes the events above.
- Native approval prompts, user dialogs, and hook callbacks are refused with an error answer, so a
  turn never blocks on them. Both harnesses run with approvals off inside the sandbox.

## Not covered yet

- Read-only follower attachments; only one attachment per session.
- Log compaction or retention; a session log grows for the session's lifetime.
- Transport security; the listener is plaintext on loopback.
- Recovery semantics for a turn lost mid-tool beyond reporting `PROCESS_LOST`.
