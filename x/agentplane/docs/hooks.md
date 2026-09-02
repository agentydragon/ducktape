# Harness hooks

What each harness offers a driver of its JSON protocol when a hook fires, how fast the driver
has to answer, and what that fixes about a hook capability on the runner protocol. The findings
come from reading the pinned Claude Code (2.1.252) and Codex `main` at `312709252d`
(`rust-v0.152.1`); no capture has exercised them yet. The scripted tests and the live probe run
with hooks off ([roster § Deliberately unsupported](../native/docs/protocol_roster.md)), and the
runner refuses the one callback it receives ([SPEC](../runner/SPEC.md) § What the harnesses do
not promise).

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

## Reaction budget, side by side

| Harness     | Who runs the hook      | Driver sees            | Can veto a tool         | Default budget | On timeout                           |
| ----------- | ---------------------- | ---------------------- | ----------------------- | -------------- | ------------------------------------ |
| Claude Code | The driver, over stdio | `hook_callback`        | Yes, with reason        | 600 s          | Tool skipped, model told (≥ 2.1.210) |
| Codex       | A child process        | `hook/*` notifications | Only via the shell hook | 600 s          | Tool runs                            |

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
