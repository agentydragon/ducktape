# Claude Code hook semantics

Upstream Claude Code hook behavior that the hook daemon's models and guards
depend on, established by auditing the upstream source (2026-05). Fine detail
may drift with upstream releases — re-verify against the current binary before
relying on it, starting from the files below.

## Key upstream source files

- `src/types/hooks.ts` — hook result/output types, Zod schemas for sync/async responses
- `src/services/tools/toolHooks.ts` — `resolveHookPermissionDecision()`, pre/post tool hook runners
- `src/utils/hooks.ts` — main hook execution engine, `processHookJSONOutput()`, all `execute*Hooks()` functions
- `src/entrypoints/sdk/coreSchemas.ts` — Zod schemas for all hook input types
- `src/entrypoints/sdk/coreTypes.ts` — `HOOK_EVENTS` array, `EXIT_REASONS`
- `src/utils/hooks/sessionHooks.ts` — session-scoped function hooks
- `src/utils/messageQueueManager.ts` — unified command queue for message injection
- `src/bridge/` — WebSocket bridge to claude.ai web UI

## REPL vs non-REPL hooks

Upstream splits hooks into two execution paths:

**REPL hooks** — run during the model conversation loop. `systemMessage` is
injected as a `hook_system_message` attachment the model reads.

**Non-REPL hooks** — run outside the loop (startup, shutdown, filesystem events).
`systemMessage` goes to UI notification callback only; model never sees it.

Non-REPL hooks: SessionStart, SessionEnd, Setup, CwdChanged, FileChanged,
InstructionsLoaded, WorktreeCreate, WorktreeRemove, ConfigChange.

Everything else is REPL.

Upstream code path: REPL → `executeHooks()` yields messages into conversation.
Non-REPL → `executeHooksOutsideREPL()` returns `HookOutsideReplResult`.

Our guard: `server.py` raises `AssertionError` if `system_message` is set on a
non-REPL hook output.

## Async hooks

Three flavors upstream:

1. **Config-based async** (`"async": true` in hook config) — process is
   backgrounded immediately, returns empty output, no conversation effect.

2. **Runtime async** (hook emits `{"async": true}` as first stdout line) —
   dynamically decides to go async. Registered in `AsyncHookRegistry`.

3. **`asyncRewake`** (`"asyncRewake": true` in hook config) — background
   process that on exit code 2 calls `enqueuePendingNotification()` to inject
   a single `task-notification` into the command queue. One-shot: one process
   exit → one message. Bypasses `AsyncHookRegistry` entirely.

## Message injection mechanisms

No HTTP/UDS listener for arbitrary message injection. All paths into the
conversation:

| Mechanism                 | Auth                | How                                                           |
| ------------------------- | ------------------- | ------------------------------------------------------------- |
| Sync hooks                | Local (hook config) | Return `systemMessage` / `additionalContext` in JSON response |
| `asyncRewake` hooks       | Local (hook config) | Exit code 2 → `enqueuePendingNotification()`                  |
| Bridge WebSocket          | Anthropic OAuth     | `wss://<host>/v1/session_ingress/ws/<session_id>`             |
| Agent SDK DirectConnect   | SDK auth token      | WebSocket to server-provided `ws_url`                         |
| MCP channel notifications | MCP server config   | Allowlisted MCP servers push notifications                    |
| Our session mailbox       | In-process only     | `session.post_message()` flushed on next REPL hook            |

Bridge and DirectConnect are not usable from local processes in web containers
(require Anthropic OAuth / SDK auth respectively).

## Hook types (settings.json)

| Type      | What                            |
| --------- | ------------------------------- |
| `command` | Shell command (bash/powershell) |
| `prompt`  | LLM prompt evaluation           |
| `agent`   | Agentic verifier                |
| `http`    | HTTP POST to URL                |

Plus internal-only **function hooks** (`addFunctionHook()`) — in-process
TypeScript callbacks, session-scoped, ephemeral. Not user-configurable.

No native MCP-as-hook-callback support. `http` type is the closest workaround
(MCP server implements a plain webhook endpoint).

## PreToolUse permission semantics

- `permissionDecision: 'allow'` does NOT bypass settings.json deny/ask rules.
  `checkRuleBasedPermissions()` still runs. Deny wins over hook allow.
- Multiple hooks: **deny > ask > allow** precedence.
- `updatedInput` without `permissionDecision` → passthrough (modifies input,
  normal permission flow still applies).
- `updatedInput` with `deny` → silently dropped.
- `updatedInput` + `allow` satisfies `requiresUserInteraction` (headless escape hatch).

## Other notable semantics

- **Exit code 2** = blocking error for shell command hooks (equiv to `decision: 'block'`).
- **SessionEnd timeout**: 1.5s default (not 10min). Override via `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`.
- **`CLAUDE_ENV_FILE`**: only on SessionStart, Setup, CwdChanged, FileChanged hooks.
- **`updatedMCPToolOutput`**: silently ignored for non-MCP tools.
- **`hookSpecificOutput.hookEventName`** validated against expected event — mismatch throws.
- **Workspace trust**: all hooks skipped if trust dialog not accepted (interactive mode).
- **`tool_input`**: upstream uses `z.unknown()`, we use `dict[str, Any]`.

## Session ID lifecycle

| Event                  | New session ID?                         | SessionStart fires? | `source`    |
| ---------------------- | --------------------------------------- | ------------------- | ----------- |
| Fresh startup          | Yes (`randomUUID()`)                    | Yes                 | `'startup'` |
| `/clear`               | Yes (`regenerateSessionId()`)           | Yes                 | `'clear'`   |
| Compaction             | No — keeps existing                     | Yes                 | `'compact'` |
| `/resume` / `--resume` | No — restores old via `switchSession()` | Yes                 | `'resume'`  |
| `--continue`           | No — restores old                       | No (skipped)        | —           |

- **Only `/clear` generates a new session ID.** Old ID saved as `parentSessionId`.
- **Compaction** keeps the same session ID and transcript file. The `'compact'`
  source is informational. Our daemon observed Setup vs SessionStart having
  mismatched IDs during compaction — this is a Claude Code bug, not design.
- **Resume** from a new Claude instance: process generates a fresh UUID at
  startup, then `switchSession()` overwrites it with the old ID before hooks fire.
- **Transcripts**: `~/.claude/sessions/<sessionId>.jsonl`. Metadata (title, tags)
  appended to end of same file. No separate metadata store.
