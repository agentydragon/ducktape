# Harness hooks

What each harness offers a driver of its JSON protocol when a hook fires, how fast the driver
has to answer, and what that fixes about a hook capability on the runner protocol. The findings
come from reading the pinned Claude Code (2.1.252) and Codex `main` at `312709252d`
(`rust-v0.152.1`), and from the capture probe's `hooks` and `hooks_deny` scenarios
([capture README](../capture/README.md)) run against LiteLLM on the cheap-experiments key
(Haiku 4.5 through the Anthropic messages route, `gpt-5.6-luna` through the responses route):
one shell tool call per turn, every registered hook answered by the probe as it arrived. The
scripted tests run with hooks off ([roster § Deliberately unsupported](../native/docs/protocol_roster.md)),
and the runner refuses the one callback it receives ([SPEC](../runner/SPEC.md) § What the
harnesses do not promise).

## Claude Code: the driver is the hook

- **Registration** is the `hooks` field of the `initialize` control request: event → matchers →
  `hookCallbackIds`. It happens once, at the handshake, before the first turn; nothing
  re-registers mid-session.
- **Execution**: when a registered event fires, the CLI sends a `hook_callback` control request
  on the same stdio stream, carrying the callback id and the hook input, and waits for our
  `control_response`. No shell command runs.
- **Blocking events**: `PreToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`.
  `PostToolUse`, `Notification`, `SessionStart`, `SessionEnd`, `PermissionRequest` are
  observe-only. A `PreToolUse` answer allows, denies with a reason the model sees, replaces the
  tool input, or adds context.
- **Budget**: 600 s for most events, 30 s for `UserPromptSubmit`, 1.5 s shared by all
  `SessionEnd` hooks. Since 2.1.210 a `PreToolUse` that times out skips the tool and tells the
  model the hook did not answer; the turn continues.
- **Order**: `PreToolUse` runs before the permission check, so a deny means no `can_use_tool`
  request follows.
- **Visibility**: `--include-hook-events` adds `hook_started`, `hook_progress`, `hook_response`
  system frames to the stream.
- **Gotcha**: `--safe-mode`, which the scenarios use to keep plugins out of the prompt, also
  disables hooks. A hooks scenario has to drop it and rely on `--setting-sources=` alone.
- **Observed live.** In a turn with one Bash call the callbacks came in this order, each
  answered by the probe within the same millisecond: `UserPromptSubmit` 27 ms after the prompt
  and before `system/init`; `PreToolUse` 4 ms after the assistant frame carrying the
  `tool_use`; `PostToolUse` after the command ran and before the `tool_result` user frame;
  `Stop` after the final assistant text and 2 ms before `result`. No `SessionStart` or
  `SessionEnd` callback fired for a registration made at `initialize`, and no
  `hook_started`/`hook_progress`/`hook_response` frames appeared under `--include-hook-events`:
  those report command hooks, not SDK callbacks. Every callback carries a top-level
  `tool_use_id`, an id per firing even for events without a tool.
- **Deny, observed.** `permissionDecision: deny` with a reason: no `can_use_tool` followed, the
  model received a `tool_result` with `is_error: true` whose content is the reason verbatim,
  the turn continued, and the assistant reported the reason. Allow: no `can_use_tool` either,
  the tool ran. The whole turn took 4 s in both cases.

## Codex: the hook is a shell command

- **Registration** is `[[hooks.<Event>]]` in `config.toml`, which `thread/start` accepts per
  thread through its config map. Twelve events, with Claude-shaped input JSON on the command's
  stdin. Hooks are on by default.
- **Execution**: Codex spawns the command itself. The driver sees `hook/started` and
  `hook/completed` notifications (and can ask `hooks/list`); nothing arrives as a request to
  answer.
- **Synchronous veto points** the driver does get are the approval requests for command
  execution, file changes, and permissions. They block until answered or the turn is interrupted.
- **Budget**: 600 s by default; `SessionEnd` and interrupt-time hooks get 1 s, capped at 3 s.
- **Fail-open**: a hook that exits non-zero or times out is logged and the tool runs.
- **Trust**: an unmanaged hook is untrusted until the hash recorded in the hooks state matches
  its command; `thread/start` takes `bypass_hook_trust` for a driver that owns the config.
- **Observed live.** Hooks passed through `thread/start`'s config map run from a
  `<session-flags>/config.toml` layer, and all six registered events ran: `SessionStart` 41 ms
  after `thread/start`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and
  `SessionEnd` on close. A Python hook command took 37 to 56 ms per run; the driver saw a
  `hook/started`/`hook/completed` pair per run with the duration and an `entries` list for
  errors and feedback. The thread's first frames warn that the trust bypass is on and that the
  `SessionEnd` timeout is clamped to 3 s.
- **Allow is not a decision.** A `PreToolUse` answer with `permissionDecision: allow` is
  rejected ("returned unsupported permissionDecision:allow"), the run is marked `failed`, and the
  tool runs anyway, which is the fail-open rule at work. A hook that has no objection answers
  `{}`.
- **Deny, observed.** `permissionDecision: deny` with a reason: the run is `blocked` with a
  `feedback` entry carrying the reason, the command never ran, and the model's tool outcome
  read "Command blocked by PreToolUse hook: <reason>. Command: <command>"; the assistant
  reported it and the turn completed.
- **Input keys** beyond `session_id`, `cwd`, `transcript_path` (null on an ephemeral thread),
  `model`, `permission_mode`, `hook_event_name`: `SessionStart` has `source`;
  `UserPromptSubmit` `prompt`, `turn_id`; `PreToolUse` `tool_name`, `tool_input`,
  `tool_use_id`, `turn_id`; `PostToolUse` adds `tool_response`; `Stop` `last_assistant_message`,
  `stop_hook_active`, `turn_id`; `SessionEnd` `reason`.

## Reaction budget, side by side

| Harness     | Who runs the hook      | Driver sees            | Can veto a tool               | Default budget | On timeout                           |
| ----------- | ---------------------- | ---------------------- | ----------------------------- | -------------- | ------------------------------------ |
| Claude Code | The driver, over stdio | `hook_callback`        | Yes, with reason              | 600 s          | Tool skipped, model told (≥ 2.1.210) |
| Codex       | A child process        | `hook/*` notifications | Deny only, via the shell hook | 600 s          | Tool runs                            |

## What a hook capability on the runner protocol must satisfy

Hooks are negotiated per session, not assumed: a client that wants them declares them in `Open`
and the runner reports which it could register, so a client never learns after the fact that
its veto was decorative.

- **Registration carries the failure policy.** Per hook: the events, a deadline, and one of
  `fail_open`, `fail_closed` (deny, with a reason the model sees), or `detach` (stop taking
  hooks for the session). The runner enforces the deadline itself, well under either harness's
  600 s, so a slow client never falls into the harness's own timeout with its less useful
  default.
- **Claude maps directly.** The runner registers the client's events at `initialize`, forwards
  each `hook_callback` as a request on the `Attach` stream, and answers the harness with the
  client's decision.
- **Codex needs the runner to be the shell hook.** A small command shipped in the runner image
  calls back into the runner over a local socket; the runner asks the client over `Attach` the
  same way and turns the answer into the command's exit and stdout. `bypass_hook_trust` is set,
  since the runner authored the config.
- **Log before answering.** The hook's request and the client's decision are session-log events
  written before the harness gets its answer, so a trajectory shows what was asked and decided.
- **Webhooks sit above the runner.** HTTP callback registration belongs to the app, which holds
  the client side of the capability and fans out; the runner protocol only ever has one
  attachment to ask.

The first hook worth wiring is `PreToolUse` for credentialed egress: a deny with a reason is a
far better signal to the agent than a 403 from the proxy.
