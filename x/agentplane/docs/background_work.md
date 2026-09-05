# Background work

What each harness tells the driver, and the model, about work the agent left running past the turn
that started it — and what a common abstraction over the two could honestly promise.

Evidence as in [driver_tools.md](driver_tools.md): **confirmed** means observed by driving the
pinned Claude Code 2.1.252 or Codex app-server 0.152.0 binary against a loopback model endpoint;
**read** means taken from Codex's Rust, the `@anthropic-ai/claude-agent-sdk` type declarations, or
the debundled `cli.js`.

## Claude Code: a session-scoped task registry

Background work is a first-class registry with ids, types and a lifecycle, not a property of the
tool call that started it.

**What the model gets.** `Bash` takes `run_in_background`; `Agent` takes it too and defaults to
running in the background. The tool result for a backgrounded command is the task's id and the file
its output is accumulating in:

```text
Command running in background with ID: bb810g1i0. Output is being written to:
…/tasks/bb810g1i0.output. You will be notified when it completes. To check interim output,
use Read on that file path.
```

The model reads that file, or calls `BashOutput`, `TaskOutput({task_id, block})` or
`TaskStop({task_id})`. When a task settles the CLI injects a `<system-reminder>` user turn into the
transcript carrying the notification, so the model learns of the completion in-band at its next
turn without the driver doing anything. Confirmed end to end with a command that finished between
two turns.

**What the driver gets.** A level signal plus edges, all as `system` frames:

- `background_tasks_changed` carries the complete live set on every membership change, each entry
  `{task_id, task_type, description, ambient?}`. REPLACE semantics: swap the set for the payload
  rather than pairing edges, so a missed edge cannot wedge a stale indicator. Nothing is emitted at
  startup; a repeated `initialize` on a running process is answered with a snapshot behind the
  success response (read).
- `task_started` (`task_id`, `tool_use_id`, `description`, `task_type`, `is_backgrounded`),
  `task_updated` (a `patch` of the changed fields), `task_notification` (`status` one of
  `completed`/`failed`/`stopped`, `output_file`, `summary`, optional `usage`), and `task_progress`.
  All confirmed except `task_progress`, which no scenario here produced.

The session event queue is capped at 1000 and preferentially retains lifecycle bookends and
terminal status under pressure. Draining stamps fresh outer `uuid`/`session_id` values, and a
terminal notification is guarded for once-only delivery (read). On process restart, consume the
next `background_tasks_changed` as a replace/reset rather than replaying retained edges into old
state. See [claude_runtime_contracts.md](claude_runtime_contracts.md) for the surrounding recovery
constraints.

**What the driver can do.** `stop_task {task_id}` kills one task — confirmed, and it produced
`background_tasks_changed` with an empty set, `task_updated {status: "killed"}` and
`task_notification {status: "stopped"}`. The `background_tasks` control request moves in-flight
_foreground_ work to the background (all of it, or the one task whose `tool_use_id` is given), the
control equivalent of Ctrl+B; each blocking tool call returns a "running in the background"
tool_result at once and the turn continues (read). The reply is `{backgrounded: <result>}` for the
single-task form and `{}` for the all-tasks form (read); confirmed only as a success for the
all-tasks form, with nothing foreground to move. Declaring `perTaskStopAffordance` at `initialize` tells the CLI the driver
renders a per-task stop, which spares running background tasks from a session interrupt; absent, an
interrupt kills them (read).

Reading a task's output is not a control request: the driver reads the path in
`task_notification.output_file`.

`Stop` and `SubagentStop` hook inputs carry `background_tasks: BackgroundTaskSummary[]` alongside
`session_crons`, so a hook can tell "the session is done" from "the session is waiting on
background work" (read).

## Codex: live exec sessions, and nothing above them

Codex has no registry of agent background work. What it has is the set of PTY sessions
`exec_command` left running.

**What the model gets.** `exec_command` returns a session id when the command outlives
`yield_time_ms`, and `write_stdin {session_id, chars, yield_time_ms}` writes to it and/or polls for
output. The tool output is plain text:

```text
Chunk ID: 5b903e
Wall time: 1.0005 seconds
Process running with session ID 78818
Original token count: 0
Output:
```

**The model is told nothing when the process exits.** With the process finishing while the thread
was idle between turns, the next turn's `input` carried no notice of the exit and none of the
output. Confirmed. The model only learns anything by calling `write_stdin` again.

**What the driver gets and can do.** Three experimental-gated methods, all per thread:

| Method                                 | Result                                                                  |
| -------------------------------------- | ----------------------------------------------------------------------- |
| `thread/backgroundTerminals/list`      | paginated `{itemId, processId, command, cwd, osPid, cpuPercent, rssKb}` |
| `thread/backgroundTerminals/terminate` | `{terminated: bool}`, by `processId`                                    |
| `thread/backgroundTerminals/clean`     | drops all of them (read; not exercised)                                 |

Confirmed: a `sleep 120` appeared as
`{itemId: "call-3", processId: "44371", command: "sleep 120", cwd: …}` with `osPid`, `cpuPercent`
and `rssKb` null on this platform; it survived `turn/completed`; `terminate` returned
`{terminated: true}` and the next list was empty.

**There is no push notification for membership.** No notification method in the v2 protocol reports
a background terminal starting or ending, and none arrived. What the driver does get is
item-scoped: `item/commandExecution/outputDelta` and `item/completed` for the originating
`exec_command` item, which arrived after the process exited while the thread was idle. Confirmed. A
driver that wants the current set has to poll the list.

Codex's `process/{spawn,writeStdin,kill,resizePty}` with `process/outputDelta` and `process/exited`,
and the `command/exec*` family, are processes the **driver** starts through the app-server, not work
the agent started; they do not appear in the background-terminal list (read).

## What this means for the runner protocol

The two are not the same object, and the gap is not cosmetic.

The floor both harnesses reach is: enumerate the live background work of a session, identify each
entry by a harness id, correlate it to the tool call that started it (Claude `tool_use_id`, Codex
`itemId`), and stop one by id. A `ListBackgroundWork` / `StopBackgroundWork` pair at that floor is
implementable on both, as confirmed above.

Four things are Claude-only and cannot be synthesized:

- **Push on membership change.** Claude's `background_tasks_changed` is a level signal the driver
  can trust; on Codex the runner would have to poll `thread/backgroundTerminals/list` and diff, and
  a diff between polls is not the same guarantee.
- **The model being told.** Claude injects the completion into the transcript. Nothing on Codex
  does, and the runner cannot inject it either without writing into the thread's input, which is a
  product-visible act, not an adapter detail.
- **Captured output.** Claude names an output file per task. On Codex the driver would have to
  reconstruct output from `item/commandExecution/outputDelta` deltas it happened to be listening
  for.
- **Non-shell background work.** Claude backgrounds subagents, workflows and MCP tasks and reports
  each with its own `task_type`. Codex's list holds shell sessions only.

Per [common_protocol.md](common_protocol.md) — "a related operation on both sides is not evidence
that the two are equivalent" — the honest shape is the floor pair as common verbs, with Claude's
push signals, output file and in-band model notice staying provider-native. Defining a common
"background task completed" event would either be a Claude passthrough dressed as a shared
guarantee, or force the runner to poll Codex and manufacture an edge the harness never emitted.
