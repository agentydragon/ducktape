# Hook Semantics Audit

Notes from auditing our `devinfra/claude/claude_api/hooks/` models against the
upstream Claude Code source at `/code/github.com/anthropics/claude-code-leaked/`.

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

## Startup hook sequence (web session)

1. **Setup** (non-REPL, only with `--init`/`--init-only`) — `systemMessage` → UI only,
   `additionalContext` → model sees via attachment
2. **SessionStart** (non-REPL) — `systemMessage` → UI only, but
   `hookSpecificOutput.additionalContext` → **model sees** (becomes
   `hook_additional_context` attachment in initial conversation).
   `initialUserMessage` → becomes an actual user turn.
   Our Mako template output goes into `additionalContext`, so the model reads it.
3. **InstructionsLoaded** (×N, non-REPL) — fire-and-forget, observability only,
   no output processed
4. **UserPromptSubmit** (REPL) — first hook where `systemMessage` reaches the model
5. **PreToolUse** (REPL) — can modify input, block, inject context
6. **PostToolUse** (REPL) — can inject context, replace MCP output

SessionStart runs async (fire-and-forget) in interactive mode. In print mode
(`-p`) it's awaited. ConfigChange does NOT fire during startup.

## Per-session boot sequence (web environment)

Complete timeline of what happens when a Claude Code web session starts,
combining environment-manager RE findings with Claude Code TypeScript source
analysis.

### Actors

- **environment-manager** (`/usr/local/bin/environment-manager`): Go binary,
  runs as PID 1 or early process. Garble-obfuscated. Manages environment
  setup before and alongside Claude Code.
- **Claude Code** (`claude`): TypeScript CLI. Launched twice: once by RunInit
  (`--init-only`), once as the main session (`--resume=<url>`).

### Timeline

```
┌─ environment-manager starts ──────────────────────────────────────────────┐
│                                                                           │
│  1. Manager.Run() launches 3 goroutines:                                  │
│     a. registerMCPServersAsync (parallel)                                 │
│     b. initializeEnvironmentAsync (parallel) ← runs Initialize           │
│     c. addOfficialPluginMarketplaceAsync (parallel)                       │
│                                                                           │
│  2. Manager.configureEnvironment() (synchronous):                         │
│     - SetStartupContext, SetAuthContext, SetSessionMode on envType         │
│     - configureGitSigning (/tmp/code-sign symlink)                        │
│                                                                           │
│  3. initializeEnvironmentAsync runs anthropicEnvironmentType.Initialize:   │
│                                                                           │
│     ┌─ Initialize ────────────────────────────────────────────────────┐    │
│     │                                                                 │    │
│     │  3a. [new/setup-only only] Install languages (Go, Node, Python) │    │
│     │  3b. [new/setup-only only] Clone git sources                    │    │
│     │  3c. [all modes] Run init script (if configured)                │    │
│     │                                                                 │    │
│     │  3d. [all modes] RunInit: `claude --init-only`  ◄── FIRST HOOK │    │
│     │      │                                              INVOCATION  │    │
│     │      │  Claude Code --init-only does:                           │    │
│     │      │  i.   applyConfigEnvironmentVariables()                  │    │
│     │      │  ii.  processSetupHooks('init', sync)                    │    │
│     │      │       → executes Setup hooks from settings.json          │    │
│     │      │       → (our settings.json has NO Setup hooks → noop)    │    │
│     │      │  iii. processSessionStartHooks('startup', sync)          │    │
│     │      │       → executes SessionStart hooks from settings.json   │    │
│     │      │       → fires our SessionStart hook → hook daemon starts │    │
│     │      │  iv.  gracefulShutdownSync(0) → process exits            │    │
│     │      │                                                          │    │
│     │      └─ RunInit errors are NON-FATAL (logged, not returned)     │    │
│     │                                                                 │    │
│     │  3e. [all modes] bootstrapClaudeSkills                          │    │
│     │      → writes SKILL.md to ~/.claude/skills/session-start-hook/  │    │
│     │      → writes SKILL.md to /home/claude/.claude/skills/...       │    │
│     │                                                                 │    │
│     │  3f. [all modes] bootstrapHooksInAllDirs                        │    │
│     │      → writes settings.json to ~/.claude/ (if not exists)       │    │
│     │      → writes stop hook script to ~/.claude/ (if not exists)    │    │
│     │      → same for /home/claude/.claude/                           │    │
│     └─────────────────────────────────────────────────────────────────┘    │
│                                                                           │
│  4. ClaudeCodeExecutor.Execute() launches main session:                   │
│     `claude --output-format=stream-json --verbose --replay-user-messages  │
│            --input-format=stream-json --debug-to-stderr                   │
│            [--<key>=<value> ...from ClaudeCodeArgs]                       │
│            --resume=<session_ingress_url>`                                │
│                                                                           │
│     ┌─ Claude Code main session startup ─────────────────────────────┐    │
│     │                                                                 │    │
│     │  5a. processSessionStartHooks('startup')  ◄── SECOND HOOK      │    │
│     │      → fires SessionStart hooks again                INVOCATION │    │
│     │      → our hook daemon receives second SessionStart             │    │
│     │      → idempotent: proxy already running, env already set       │    │
│     │                                                                 │    │
│     │  5b. InstructionsLoaded (xN) — fire-and-forget                  │    │
│     │  5c. Begin conversation loop                                    │    │
│     │      - UserPromptSubmit, PreToolUse, PostToolUse, etc.          │    │
│     └─────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────────┘
```

### New vs resume sessions

| Step                    | new / setup-only    | resume / resume-cached  |
| ----------------------- | ------------------- | ----------------------- |
| Install languages       | Yes                 | Skipped                 |
| Clone git sources       | Yes                 | Skipped                 |
| Run init script         | Yes (if configured) | Yes (if configured)     |
| RunInit (`--init-only`) | Yes                 | Yes                     |
| Bootstrap skills/hooks  | Yes                 | Yes (idempotent writes) |
| Main session `--resume` | Yes                 | Yes                     |

All modes run RunInit and bootstrap skills/hooks. The only difference is that
resume modes skip language installation and source cloning (the filesystem
state from the prior session is assumed intact).

### Hook double-firing

Hooks fire TWICE per session:

1. **From `claude --init-only`** (step 3d): synchronous, runs in the
   environment-manager's Initialize goroutine. Fires Setup hooks (none
   configured in our settings.json) and SessionStart hooks.

2. **From the main `claude --resume` session** (step 5a): the main session's
   startup path fires SessionStart hooks again. Whether this is synchronous
   or fire-and-forget depends on the mode:
   - Interactive: fire-and-forget (non-blocking)
   - Print (`-p`): awaited
   - The `--resume` flag itself does NOT suppress SessionStart hooks
     (only `--continue` does — see session ID lifecycle table above)

Our hook daemon is designed for this: the second SessionStart invocation
hits the already-running daemon, which treats it as an idempotent re-entry
(proxy already started, env file already written, pre-commit already
installed).

### `claude init` vs `--init` vs `--init-only`

| Invocation           | What it is                   | Behavior                                                                                                                    |
| -------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `claude init`        | Positional prompt arg "init" | Launches interactive REPL with "init" as user message. NOT what env-manager uses.                                           |
| `claude --init`      | Hidden CLI flag              | Runs Setup hooks with 'init' trigger, then **continues** into interactive REPL. Would hang in env-manager context (no TTY). |
| `claude --init-only` | Hidden CLI flag              | Runs Setup + SessionStart hooks synchronously, then **exits**. This is what env-manager uses.                               |
| `/init`              | Slash command (in-session)   | Prompt-type command that generates CLAUDE.md. Completely unrelated.                                                         |

The environment-manager runs `claude --init-only` (confirmed by string length
0xb = 11 in binary, matching `--init-only`). The `--init` flag without
`-only` would be unsuitable because it enters the REPL and never exits.

### Remaining uncertainties

- **Exact binary address of RunInit call in 495ea204**: The addresses shifted
  between binary versions due to garble recompilation. The a6f96673 addresses
  (0xae0b00 etc.) are documented but may not match the current binary.
  Garble's string encryption prevents direct string-based address correlation.

- **Ordering of RunInit vs settings.json write**: RunInit (step 3d) runs
  BEFORE bootstrapHooksInAllDirs (step 3f). However, this is benign: both
  `~root/.claude/settings.json` and `~claude/.claude/settings.json` are baked
  into the container image (from `rootfs/`). The env-manager's
  bootstrapHooksInAllDirs checks `os.Stat()` and skips writing if the file
  exists, so step 3f is a no-op on normal runs. The `--init-only` invocation
  always sees our hooks configuration.

## Changes made (commit 591e6d2d8)

- Added `PermissionDeniedInput/Output` (with `retry` field)
- Added `bypass_permissions_disabled` to `SessionEndReason`
- Fixed `WorktreeCreate/RemoveInput` to inherit `HookInputBase`
- Added runtime guard against `system_message` on non-REPL hooks
- Centralized semantics documentation in `common.py`
