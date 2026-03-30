# Hook Call Patterns in Claude Code Web

Observed patterns and issues with Claude Code hook event delivery.

**Captured**: 2026-03-30 (Claude Code session `cse_01ANqoTWWCxF71H5Aq2DqwnT`).

## Session Timeline (This Container)

All sessions in container `container_019njrrfmoCSVka1MjwyXMzt`, VM lifetime ~3h.

| Time (UTC) | Session ID | Event           | `source`     | Notes                             |
| ---------- | ---------- | --------------- | ------------ | --------------------------------- |
| 07:15:28   | `3409056f` | SessionStart    | `startup`    | First session, daemon on same ID  |
| 07:15:29   | `3409056f` | SessionEnd      | —            | Immediately ended (brief session) |
| 19:05:51   | `1c9fe809` | Daemon startup  | —            | New session, new daemon           |
| 19:06:04   | `1c9fe809` | SessionStart    | `startup`    | Full setup (13s), proxy/docker    |
| 19:06:16   | `1c9fe809` | PreToolUse      | —            | Normal operation begins           |
| …          | `1c9fe809` | Pre/PostToolUse | —            | ~48 min of tool calls             |
| 19:54:04   | `1c9fe809` | Stop            | —            | Agent stopped                     |
| 19:58:07   | `1c9fe809` | SessionEnd      | —            | Session closed (4 min after Stop) |
| 22:04:59   | `1ee260ef` | Daemon startup  | —            | Subagent daemon (no SessionStart) |
| 22:04:59   | `3a1f5b5a` | Daemon startup  | —            | Main daemon (same millisecond)    |
| 22:05:00   | `3a1f5b5a` | SessionStart    | **`resume`** | Resumed session, full re-setup    |
| 22:05:14   | `3a1f5b5a` | SessionStart OK | —            | Setup completed (14s)             |
| 22:12:57   | `3a1f5b5a` | Stop            | —            | Agent stopped                     |

### Key Observations

1. **Resume creates new session ID**: When Claude Code resumes a session
   (e.g., after page refresh or reconnect), a new internal session UUID
   is assigned. The hook receives `source: "resume"` (not `"startup"`
   or `"compact"`).

2. **Two daemons on resume**: `1ee260ef` and `3a1f5b5a` start at the
   same millisecond (22:04:59.575). `1ee260ef` only receives
   `InstructionsLoaded` hooks (4 of them); `3a1f5b5a` receives
   `SessionStart`. The former is the subagent process — it starts a
   daemon but never gets a session setup.

3. **Session ID in hook input matches daemon**: Unlike compaction
   (where the documented README says session IDs can mismatch), resume
   sends the hook to the daemon with the matching session ID.

4. **Old session paths become stale**: After resume, `1c9fe809`'s
   daemon, proxy socket, and bazelrc still exist on disk but no process
   serves them. The new session `3a1f5b5a` creates fresh paths.

5. **`CLAUDE_CODE_SESSION_ID` is stable**: The API-level session ID
   (`cse_01ANqoTWWCxF71H5Aq2DqwnT`) remains constant across resumes.
   Only the internal UUID (`session_id` in hook input) changes.

## Hook Input Payloads

### SessionStart (initial startup)

```json
{
  "session_id": "1c9fe809-b4dd-4f3f-924c-900b1ebfeaad",
  "transcript_path": ".../-home-user-ducktape/1c9fe809-b4dd-4f3f-924c-900b1ebfeaad.jsonl",
  "cwd": "/home/user/ducktape",
  "permission_mode": null,
  "model": null,
  "hook_event_name": "SessionStart",
  "source": "startup",
  "agent_type": null
}
```

### SessionStart (resume)

```json
{
  "session_id": "3a1f5b5a-0438-416c-b26d-ddcb3e5a4dad",
  "transcript_path": ".../-home-user-ducktape/3a1f5b5a-0438-416c-b26d-ddcb3e5a4dad.jsonl",
  "cwd": "/home/user/ducktape",
  "permission_mode": null,
  "model": null,
  "hook_event_name": "SessionStart",
  "source": "resume",
  "agent_type": null
}
```

Note: `source` values observed: `"startup"`, `"resume"`. Also documented
in Claude Code: `"compact"` (context window compaction).

### Caller Environment (key vars on SessionStart)

| Variable                          | Value (example)                                       |
| --------------------------------- | ----------------------------------------------------- |
| `CLAUDE_CODE_REMOTE`              | `true`                                                |
| `CLAUDE_CODE_SESSION_ID`          | `cse_01ANqoTWWCxF71H5Aq2DqwnT`                        |
| `CLAUDE_CODE_REMOTE_SESSION_ID`   | `cse_01ANqoTWWCxF71H5Aq2DqwnT`                        |
| `CLAUDE_ENV_FILE`                 | `~/.claude/session-env/<uuid>/sessionstart-hook-0.sh` |
| `CLAUDE_PROJECT_DIR`              | `/home/user/ducktape`                                 |
| `HTTPS_PROXY`                     | `http://<container>:<jwt>@<host>:<port>`              |
| `JAVA_TOOL_OPTIONS`               | `-Dhttps.proxyHost=... -Dhttps.proxyPassword=<jwt>`   |
| `DUCKTAPE_CLAUDE_HOOKS_K8S_TOKEN` | K8s SA token (static across sessions)                 |
| `DUCKTAPE_CLAUDE_HOOKS_AGE_KEY`   | Age key for SOPS (static)                             |

The `CLAUDE_CODE_SESSION_ID` (API-level, e.g., `cse_01ANqo...`) remains
constant across resumes. The hook `session_id` (UUID) changes.

## Post-Resume Environment State

After resume to session `3a1f5b5a`, the Bash tool's environment has:

```bash
DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR=/root/.claude/session-env/3a1f5b5a-...
SESSION_BAZELRC=/root/.claude/session-env/3a1f5b5a-.../bazelrc
DUCKTAPE_SESSION_START_HOOK_TS=2026-03-30T22:05:14.376331
```

The session start hook ran successfully and wrote fresh env vars. The
Bash tool sources the session env file, so all tools get the new paths.

## Problem: Bazel Server Outlives Session

The Bazel JVM server persists across session transitions (same output
base directory, keyed by workspace path hash). But its proxy
configuration is baked into the bazelrc at startup time, pointing to
the old session's UDS proxy socket:

```
build --remote_proxy=unix:/tmp/claude-hd/1c9fe809-.../remote-proxy.sock
```

After resume creates session `3a1f5b5a`, the new proxy socket is at:

```
/tmp/claude-hd/3a1f5b5a-.../remote-proxy.sock
```

The Bazel server still tries the old path → connection refused.

The bazel wrapper compounds this: it calls `update_proxy_creds` via
RPC to the daemon matching `DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR`. After
resume, the env points to the new session, so the wrapper talks to the
new daemon — but then execs `bazelisk --bazelrc=<new-session-bazelrc>`.
Since the bazelrc has a different `--remote_proxy` path, Bazel detects
a startup option change and **kills the existing server** to restart
with the new bazelrc. This loses the warm Skyframe cache (45-min cold
reload).

### Possible Fixes

1. **Fixed proxy socket path**: Use `/tmp/claude-proxy.sock` regardless
   of session ID. New daemon takes over the socket. Bazel server never
   sees a changed startup option.
2. **Restart proxy at old path**: On resume, also bind a proxy at the
   old session's socket path, so existing Bazel servers keep working.
3. **PreToolUse health check**: Detect stale proxy and restart before
   each Bazel invocation (fast, ~10ms).
4. **Accept cold restart**: Kill Bazel server on resume, pay the 45-min
   cost. Not viable for interactive sessions.

Option 1 (fixed path) is simplest and preserves the warm Skyframe
cache. The proxy socket path would be derived from the workspace
directory hash (same as Bazel's output base), not the session ID.

## `source` Values

| Value     | When                           | Observed   |
| --------- | ------------------------------ | ---------- |
| `startup` | Fresh session start            | Yes        |
| `resume`  | Reconnect (page refresh, etc.) | Yes        |
| `compact` | Context window compaction      | Documented |
| `clear`   | After `/clear` command         | Documented |

Confirmed by Zod schema in Claude Code binary (`source: z.enum(["startup",
"resume", "clear", "compact"])`) and the Pydantic model in
`devinfra/claude/claude_api/hooks/session_start.py`.

**Note**: Third-party testing (DevelopersIO) found that `--continue`
fires **both** `startup` and `resume` matchers. If hook logic should
only run on resume, account for this double firing.

## Compaction Hook Sequence

Compaction involves three hooks in sequence:

1. **`PreCompact`** — fires before compaction. Input includes
   `trigger: "manual" | "auto"` and `custom_instructions: str | null`.
2. Compaction runs (LLM summarizes conversation).
3. **`PostCompact`** — fires after compaction. Input includes
   `compact_summary` (the generated summary text).
4. **`SessionStart`** with `source: "compact"` — fires to allow
   re-injection of context lost during compaction.

**Session ID mismatch on compact**: `Setup` hook fires with the
**new** post-compaction session ID, while `SessionStart` fires with
the **old** pre-compaction session ID. This is documented in
<../README.md> ("Observed: Setup and SessionStart Use Different
Session IDs").

## Hook Event Reference

All 22 hook events are defined as Pydantic models in
<../claude_api/hooks/> with Zod schemas extracted from the Claude Code
binary at <../claude_api/hooks/schemas/>. See those files for the
canonical field definitions.

## Trace Data Location

Full hook payloads (including `tool_name`, `tool_input`, `tool_use_id`,
and the complete caller environment) are stored in OTEL traces at:

```
~/.claude/session-env/<session_id>/hook-daemon/traces.jsonl
```

Each line is a JSON span with `attributes["hook.input"]` containing the
full `HookRequest` (hook payload + caller env) and
`attributes["hook.output"]` containing the response. Written by
`server.py:handle_hook()` via `span.set_attribute()`.

## Subagent Hook Behavior

Subagents (spawned via the Agent tool) do **not** get their own
`SessionStart` or daemon. Instead:

### SubagentStart payload

```json
{
  "session_id": "1c9fe809-...", // parent session ID
  "hook_event_name": "SubagentStart",
  "agent_id": "ab66e6651c040dfd7",
  "agent_type": "Explore"
}
```

### SubagentStop payload

```json
{
  "session_id": "1c9fe809-...", // parent session ID
  "hook_event_name": "SubagentStop",
  "agent_id": "ab66e6651c040dfd7",
  "agent_type": "Explore",
  "agent_transcript_path": ".../<session>/subagents/agent-<id>.jsonl",
  "last_assistant_message": "..." // full final response
}
```

### Key observations

- Subagent events fire on the **parent** session's daemon
- `session_id` in SubagentStart/Stop is the parent's UUID, not a new one
- `agent_type` values observed: `"Explore"`, `"general-purpose"`,
  `"claude-code-guide"`
- `permission_mode` is `null` on start, changes to `"default"` or
  `"dontAsk"` on stop
- Subagent transcripts stored at `subagents/agent-<agent_id>.jsonl`
  under the parent session's project dir

### Companion process (instruction pre-loading, NOT a subagent)

On startup/resume, Claude Code spawns a **companion process** that
creates its own daemon but only receives `InstructionsLoaded` hooks.
This is the instruction pre-loading process — it reads CLAUDE.md files
to prepare the system prompt.

**This behavior is undocumented by Anthropic.** The community project
`claude-mem` independently discovered the same spawn-race pattern
([thedotmack/claude-mem#1145](https://github.com/thedotmack/claude-mem/issues/1145),
[#1447](https://github.com/thedotmack/claude-mem/issues/1447)).

**Detection**: The only env var difference between main and companion
is `CLAUDE_ENV_FILE` — present in main, absent in companion. All other
77 env vars are identical. Use `CLAUDE_ENV_FILE` presence as the signal
to run SessionStart setup.

Evidence from daemon logs:

- Starts at the **exact same millisecond** as the main process
- Receives exactly 4 `InstructionsLoaded` hooks:
  - `CLAUDE.md` (`load_reason: "session_start"`)
  - `AGENTS.md` (`load_reason: "include"`, via `@AGENTS.md` transclusion)
  - `README.md` (`load_reason: "include"`)
  - `STYLE.md` (`load_reason: "include"`)
- **No `CLAUDE_ENV_FILE`** in its environment — Claude Code did not
  set one, confirming it's not intended to run SessionStart
- Creates a minimal session-env directory (no bazelrc, no env file)
- Daemon log is only 8 lines total

| Property          | Companion process       | Agent subagent              |
| ----------------- | ----------------------- | --------------------------- |
| Gets own daemon   | Yes (separate UUID)     | No (uses parent daemon)     |
| Hook events       | `InstructionsLoaded` ×4 | `SubagentStart`/`Stop`      |
| Gets SessionStart | No                      | No                          |
| `CLAUDE_ENV_FILE` | Not set                 | N/A                         |
| Session UUID      | Own (e.g., `1ee260ef`)  | Parent's (e.g., `1c9fe809`) |
| `worker_epoch`    | Same as main process    | N/A (no separate daemon)    |

## `worker_epoch` Values

The `CLAUDE_CODE_WORKER_EPOCH` env var increments across session
transitions within the same container:

| Session    | `worker_epoch` | Event     |
| ---------- | -------------- | --------- |
| `3409056f` | (empty)        | startup   |
| `1c9fe809` | `1`            | startup   |
| `3a1f5b5a` | `3`            | resume    |
| `1ee260ef` | `3`            | companion |

Epoch `2` was not observed in traces — likely a session that started
but didn't trigger our hooks (no `.claude/settings.json` at that point,
or a brief probe).
