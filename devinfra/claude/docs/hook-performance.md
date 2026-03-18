# Hook Performance: Reducing Latency When External Services Are Down

## Problem

Every Claude Code hook invocation (PreToolUse, PostToolUse) goes through
`hook_dispatch.py`, which initializes OTEL tracing and flushes spans on exit.
When the cluster is down (OTEL endpoint unreachable, k8s API unresponsive),
each hook call blocks for up to ~5.7s — making sessions painfully slow.

### Measured Latency Breakdown

| Component                                    | Latency         | Notes                                        |
| -------------------------------------------- | --------------- | -------------------------------------------- |
| Bare python startup                          | ~14ms           | Baseline                                     |
| uvx resolution (cached wheel)                | ~200ms          | uv environment lookup + symlink setup        |
| Hook imports (pydantic, opentelemetry, etc.) | ~400ms          | Module-level imports in `hook_dispatch.py`   |
| **Full hook invocation (happy path)**        | **~600-700ms**  | uvx + imports + dispatch + handler           |
| OTEL `force_flush` (endpoint unreachable)    | **up to 500ms** | Was 5000ms, reduced in Phase 1               |
| **Full hook invocation (cluster down)**      | **~700ms**      | Was ~5700ms, OTEL skipped for frequent hooks |

The ~200ms uvx delta (vs direct python) is the cost of uv resolving the cached
environment. The ~400ms import cost is pydantic + opentelemetry SDK. The 5s OTEL
flush was the dominant problem — now fixed (Phase 1 skips OTEL for frequent
hooks, reduces flush timeout to 500ms).

### Where the OTEL Blocking Happens

1. **`otel.py:34`** — `BatchSpanProcessor(exporter)` is created on every hook
   call (line 52-53 of `hook_dispatch.py`). The processor has its own export
   thread and connection timeouts.

2. **`hook_dispatch.py:104`** — `provider.force_flush(timeout_millis=5000)`
   runs in the `finally` block on every exit. When the exporter can't reach
   the endpoint, this blocks for the full 5s.

### What Hooks Actually Do

- **PreToolUse**: Set lookup against `ALWAYS_ALLOW_COMMANDS`. Zero external
  I/O. ~0ms of handler logic.
- **PostToolUse**: Runs `pre-commit` on modified files. No external I/O
  beyond local filesystem. Handler time depends on file, but OTEL is
  irrelevant.
- **SessionStart**: Heavy — k8s secrets, proxy setup, bazelrc generation.
  OTEL tracing is actually useful here.

PreToolUse and PostToolUse do not need OTEL. They produce one trivial span
each, and losing those spans is acceptable.

## Current State

**Phase 1 is implemented** (commit `15f2efe`). OTEL is skipped for
PreToolUse/PostToolUse, and the flush timeout is 500ms (reduced from 5000ms).

## Alternatives

### A: Quick Fix — Skip OTEL for Frequent Hooks (**DONE**)

**Change**: In `hook_dispatch.py`, only initialize OTEL for `SessionStart`.
Skip `init_from_config` and `force_flush` for `PreToolUse`/`PostToolUse`.

**Implementation** (in `hook_dispatch.py`):

```python
_HOOKS_WITH_OTEL: set[type] = {SessionStartHookInput}

should_trace = type(parsed) in _HOOKS_WITH_OTEL
if should_trace:
    config = HookConfig.load_from_repo(cwd)
    if config and config.otel and config.otel.endpoint:
        otel.init_from_config(config.otel)
```

The `otel.flush()` call runs unconditionally in the `finally` block but is
a no-op when no SDK `TracerProvider` was configured — it checks
`isinstance(provider, TracerProvider)` and returns early for the default
`ProxyTracerProvider`.

Flush timeout reduced to 500ms (in `otel.py:DEFAULT_FLUSH_TIMEOUT_MS`),
not 2000ms as originally planned — 500ms is sufficient for a healthy
nearby endpoint, and a warn-and-continue approach avoids blocking even
for SessionStart.

**Pros:**

- ~30 minutes to implement. 3 files, <20 lines changed.
- Per-hook latency drops from ~5700ms to ~700ms (uvx + imports) when
  cluster is down. No regression when cluster is up.
- No new infrastructure. No new failure modes.

**Cons:**

- Still pays ~700ms per hook (uvx + imports).
- No circuit breaker — if OTEL breaks during SessionStart, it still
  blocks for up to 500ms there.
- Does not help if we later add hooks that need external services.

**Latency when cluster down:** ~700ms per PreToolUse/PostToolUse, ~1200ms for
SessionStart.

### B: File-Based State Cache

**Change**: SessionStart writes `<session_dir>/hook_state.json` with fetched
secrets, OTEL health, and circuit breaker state. Per-hook calls read this file
instead of initializing OTEL or fetching config from external services.

```python
class HookState(BaseModel):
    """Cached hook state, written by SessionStart, read by all hooks."""
    otel_endpoint: str | None = None
    otel_bearer_token: str | None = None
    otel_healthy: bool = True
    otel_last_failure_epoch: float | None = None
    k8s_secrets_fetched: bool = False
    buildbuddy_api_key: str | None = None
    created_at_epoch: float
    ttl_seconds: int = 3600
```

SessionStart atomically writes (write-to-temp + rename) this file after
completing setup. Subsequent hooks read it for circuit breaker decisions:

- If `otel_healthy=False` and last failure was <5min ago, skip OTEL entirely.
- If state file is missing, degrade gracefully (same as current behavior
  without OTEL).
- If state file is stale (past TTL), treat as if OTEL is healthy (re-probe).

Combined with Alternative A (skip OTEL for frequent hooks), the state file
adds circuit breaker awareness so even SessionStart can skip OTEL when it's
known to be broken.

**Pros:**

- Circuit breaker prevents repeated OTEL timeout during session.
- Shared state between hooks without external coordination.
- State file format is a natural backing store for a future daemon.
- Atomic writes via rename — safe for concurrent readers.

**Cons:**

- Still pays ~700ms per hook (uvx + imports).
- State can drift — no background refresh, only updated on SessionStart
  or explicit re-probe.
- Adds a new file to manage and a new code path for state read/write.

**Latency when cluster down:** Same as A for individual hooks. SessionStart
also fast on subsequent sessions (circuit breaker remembers OTEL is down).

### C: Hook Daemon on TCP Socket

**Change**: Long-running daemon managed by supervisor (same pattern as auth
proxy). Hooks send lightweight JSON RPCs over TCP. Daemon owns OTEL lifecycle,
caches state, exports spans asynchronously.

#### Architecture

```
Claude Code hook invocation
    │
    └──► hook_client.py (lightweight, ~50ms)
           │  reads stdin, POSTs to localhost:<port>, writes stdout
           │
           └──► hook_daemon (long-running, managed by supervisor)
                  │
                  ├── OTEL TracerProvider (initialized once, shared)
                  │   └── BatchSpanProcessor → async export with circuit breaker
                  ├── Cached k8s secrets (refreshed on TTL)
                  ├── Hook dispatch (PreToolUse, PostToolUse handlers)
                  └── State persistence (hook_state.json, periodic write)
```

**Daemon lifecycle**: Started by SessionStart hook as a supervisor service
(same as auth proxy). The daemon receives the `CLAUDE_ENV_FILE` path and
session configuration at startup via its supervisor environment. Since only
SessionStart has access to `CLAUDE_ENV_FILE`, the daemon must be started there
— a later-started daemon would miss the env file context and be in an
inconsistent state.

**Client**: Replaces `uvx ... claude-hook` in `.claude/settings.json`. The
client is a thin script — reads stdin, HTTP POSTs to the daemon, writes the
response to stdout. Could be a small Python script (~30 lines) or even
a compiled binary for minimal startup time.

**TCP, not UDS**: gVisor's 9p filesystem doesn't support Unix socket hard
links (same issue as supervisor). The daemon listens on
`127.0.0.1:<port>` (TCP).

**OTEL**: Initialized once at daemon startup. `BatchSpanProcessor` runs in
background, exports when buffer is full or on interval. Circuit breaker
tracks consecutive failures and stops attempting exports after N failures,
re-probing periodically. No per-hook `force_flush` — spans export
asynchronously. On daemon shutdown (supervisor SIGTERM), a single
`force_flush` with short timeout.

**Fallback**: If daemon is unreachable, client falls back to direct
`hook_dispatch.main()` (current behavior). This handles the case where
supervisor failed to start the daemon.

**Pros:**

- Per-hook latency: ~30-50ms (local HTTP + handler logic). Eliminates
  both the 200ms uvx overhead and 400ms import cost.
- OTEL is truly async — no per-hook blocking even when endpoint is down.
- Single long-lived process amortizes all startup costs.
- Natural place for background state management (secret refresh,
  health probes).

**Cons:**

- Highest implementation complexity (~300-500 lines new code).
- New daemon = new failure modes (crashes, resource leaks, port conflicts).
- Depends on supervisor being available (established pattern, but still
  a dependency).
- Daemon must be started by SessionStart — cannot be lazily started by
  other hooks because they lack `CLAUDE_ENV_FILE` access and session
  environment context.
- Client script needs to be available outside of uvx (either installed
  by SessionStart or a standalone script that doesn't need the wheel).

**Latency when cluster down:** ~30-50ms per hook. OTEL failures are
invisible.

### D: Hybrid Phased Approach (A → B → C) (**Phase 1 DONE**)

**Phase 1** (**DONE**, commit `15f2efe`): Implement Alternative A.

- Skip OTEL for PreToolUse/PostToolUse.
- Reduce SessionStart flush timeout to 500ms.
- Immediate relief for the acute problem.

**Phase 1.5** (quick win, ~2 hours): Eager venv install via Setup hook.

- The Setup hook fires once per session during `claude --init-only`
  (after container init, before SessionStart — see [invocation chain](#invocation-chain)).
- Use the Setup hook to eagerly install the hook package into a persistent
  venv at a known path (e.g., `~/.local/share/claude-hooks/venv/`).
- Reconfigure PreToolUse/PostToolUse to invoke the venv's `claude-hook`
  directly instead of going through `uvx`:

  ```json
  {
    "hooks": {
      "Setup": [{ "type": "command", "command": "uvx --from <wheel> claude-hook" }],
      "PreToolUse": [{ "type": "command", "command": "~/.local/share/claude-hooks/venv/bin/claude-hook" }],
      "PostToolUse": [{ "type": "command", "command": "~/.local/share/claude-hooks/venv/bin/claude-hook" }]
    }
  }
  ```

- Setup still pays the ~200ms uvx cost once. Subsequent hooks skip uvx
  resolution entirely, saving ~200ms per call.
- The Setup hook handler would run something like:

  ```python
  uv_venv = Path("~/.local/share/claude-hooks/venv").expanduser()
  if not uv_venv.exists():
      subprocess.run(["uv", "venv", str(uv_venv)])
      subprocess.run(["uv", "pip", "install", "--python", str(uv_venv / "bin/python"), WHEEL_URL])
  ```

- **No-op on resume**: Resume modes (`resume`, `resume-cached`) skip
  `claude --init-only`, so Setup won't fire. But the venv persists from
  the initial session, so PreToolUse/PostToolUse still work.

**Phase 2** (near-term, ~1 day): Add file-based state cache (B).

- SessionStart writes `hook_state.json`.
- Circuit breaker for OTEL health.
- Design state file format to be reusable as daemon backing store.

**Phase 3** (future, ~3-5 days): Daemon on TCP socket (C).

- Setup hook installs the package (Phase 1.5).
- SessionStart starts the daemon (needs k8s secrets, proxy config).
- PreToolUse/PostToolUse use HTTP hooks pointing at the daemon.
- Daemon reads `hook_state.json` on startup as initial state.
- Maintains in-memory state, periodically persists.
- Full latency savings realized.

**Each phase is independently valuable and shippable.** If Phase 3 never
ships, Phases 1+1.5+2 still eliminate the acute problem, remove uvx
overhead, and provide circuit breaker behavior.

## Comparison Matrix

| Criterion                       | A: Quick Fix | P1.5: Setup venv | B: State Cache        | C: Daemon        | D: Hybrid       |
| ------------------------------- | ------------ | ---------------- | --------------------- | ---------------- | --------------- |
| Per-hook latency (cluster down) | ~700ms       | ~500ms           | ~500ms                | ~50ms            | 50ms (after P3) |
| Per-hook latency (cluster up)   | ~700ms       | ~500ms           | ~500ms                | ~50ms            | 50ms (after P3) |
| SessionStart overhead change    | -4.5s        | -4.5s            | -4.5s + circuit break | -5s              | Progressive     |
| Implementation effort           | ~1h          | ~2h              | ~1d                   | ~3-5d            | Progressive     |
| New failure modes               | None         | Stale venv       | Stale state file      | Daemon lifecycle | Progressive     |
| gVisor compatibility            | N/A          | File I/O only    | File I/O only         | TCP (proven)     | All proven      |
| Backwards compatible            | Yes          | Yes              | Yes                   | Needs new client | Yes per phase   |

## Recommendation

**Alternative D (Hybrid Phased Approach).** Phase 1 is done.

Phase 1 (**done**) solved the acute pain (5s OTEL timeout on every hook)
with zero risk. OTEL is skipped for PreToolUse/PostToolUse, and flush
timeout is 500ms. The PreToolUse/PostToolUse spans are low-value
telemetry — losing them costs nothing.

**Next up**: Phase 1.5 — eagerly install hook packages into a persistent
venv via the Setup hook, eliminating the ~200ms uvx resolution cost on
every subsequent hook call. This also lays groundwork for Phase 3: the
daemon binary would be installed into the same venv.

Phase 2 adds circuit breaker durability so SessionStart also degrades
gracefully on repeat failures.

Phase 3 is the right long-term architecture for eliminating the ~500ms
baseline, but is only justified when the per-hook latency budget matters
enough to warrant the complexity. The Setup hook installs the daemon
package (Phase 1.5), SessionStart starts it with session context, and
PreToolUse/PostToolUse switch to HTTP hooks (~50ms).

## Future Extensions

### Multi-Session Daemon

Every hook invocation includes `session_id` in its JSON input. A single
daemon process could multiplex across multiple concurrent Claude Code
sessions on the same machine — maintaining per-session OTEL providers,
cached secrets, and circuit breaker state in a `dict[str, SessionState]`.

Benefits:

- One process for all sessions instead of one per session.
- Shared OTEL exporter connection pool across sessions.
- Single point for health monitoring and metrics.
- Survives individual session restarts (daemon stays up, session
  re-registers on SessionStart).

The daemon would need session lifecycle management: register on
SessionStart, clean up state on SessionEnd (or TTL expiry for
sessions that exit ungracefully).

### Cluster-Centralized Daemon

The daemon's API is just JSON-over-HTTP — there's nothing inherently
local about it. A centralized instance on the cluster could serve
multiple machines:

- Central OTEL aggregation point (one exporter, not per-machine).
- Shared k8s secret cache (fetch once, serve many sessions).
- Central circuit breaker state (if OTEL is down, all sessions
  learn immediately).
- Observability dashboard for all active sessions.

This would require:

- TLS + auth for the daemon endpoint (mTLS or bearer token).
- Network path from Claude Code containers to the cluster
  (Headscale/tailnet or proxy chain).
- Graceful degradation when the central daemon is itself unreachable
  (fall back to local dispatch).

The local daemon (Phase 3) is a natural stepping stone — it proves
the protocol and client, and the centralized version is just moving
where the daemon runs.

## Claude Code Hook Types

Claude Code supports four hook types, configured via `"type"` in
`settings.json` hook handlers.

### Command hooks (`"type": "command"`)

Runs a shell command. Event JSON arrives on stdin, results communicated
via exit code and stdout JSON. **Supported for all hook events.**

This is our current approach (uvx → `hook_dispatch.py`).

```json
{ "type": "command", "command": "uvx --from <wheel> claude-hook" }
```

### HTTP hooks (`"type": "http"`)

POSTs event JSON to a URL, reads JSON from the response body.

```json
{
  "type": "http",
  "url": "http://localhost:8080/hooks/pre-tool-use",
  "timeout": 30,
  "headers": {
    "Authorization": "Bearer $MY_TOKEN"
  },
  "allowedEnvVars": ["MY_TOKEN"]
}
```

**Only supported for a subset of events**: `PermissionRequest`, `PostToolUse`,
`PostToolUseFailure`, `PreToolUse`, `Stop`, `SubagentStop`, `TaskCompleted`,
`UserPromptSubmit`.

**NOT supported (command-only)**: `Setup`, `SessionStart`, `ConfigChange`,
`Elicitation`, `ElicitationResult`, `InstructionsLoaded`, `Notification`,
`PostCompact`, `PreCompact`, `SessionEnd`, `SubagentStart`, `TeammateIdle`,
`WorktreeCreate`, `WorktreeRemove`.

HTTP hooks that target unsupported events are skipped with a warning.

#### HTTP hook error semantics

- **2xx with empty body**: success (like exit code 0)
- **2xx with JSON body**: success, parsed as standard hook output
- **2xx with plain text**: success, text added as context
- **Non-2xx / timeout / connection failure**: non-blocking error, execution
  continues. To block an action, return 2xx with JSON containing the
  appropriate decision field (e.g., `"decision": "block"`).

### Prompt hooks (`"type": "prompt"`)

Single-turn LLM evaluation. Claude Code sends the prompt (with `$ARGUMENTS`
expanded to the hook event JSON) to a fast model and parses the response as
a hook decision. No external process, no endpoint — runs inside Claude Code.

**Fields:**

| Field     | Required | Description                                                     |
| --------- | -------- | --------------------------------------------------------------- |
| `prompt`  | yes      | Prompt text. `$ARGUMENTS` is replaced with the hook input JSON. |
| `model`   | no       | Model for evaluation. Defaults to a fast model (haiku).         |
| `timeout` | no       | Timeout in seconds.                                             |

**How it works** (verified from binary build `ab38858`):

1. `$ARGUMENTS` is replaced with the hook event JSON (literal string
   substitution via `replaceAll`). Indexed substitution (`$ARGUMENTS[0]`,
   `$1`, `$2`, named `$argName`) is also supported. If no substitution
   occurs and input exists, it's appended on a new line.
2. The expanded prompt is sent as a **user message** to an API call with a
   **system prompt**:

   ```
   You are evaluating a hook in Claude Code.

   Your response must be a JSON object matching one of the following schemas:
   1. If the condition is met, return: {"ok": true}
   2. If the condition is not met, return: {"ok": false, "reason": "Reason for why it is not met"}
   ```

3. The call uses **`outputFormat: { type: "json_schema" }`** (structured
   outputs / constrained decoding) with the schema
   `{ ok: boolean, reason?: string }`. This constrains the model to emit
   valid JSON — no markdown fences.
4. Thinking is disabled (`thinkingConfig: { type: "disabled" }`).
5. The response text is parsed with a JSON parser that strips a leading
   UTF-8 BOM if present, then validated against a Zod schema
   (`{ ok: boolean, reason?: string }`).
6. Default timeout: 30 seconds. Default model: `ANTHROPIC_SMALL_FAST_MODEL`
   env var, or the built-in fast model (haiku).
7. If the conversation has prior messages (the `messages` parameter from
   the hook executor), they are prepended before the expanded prompt
   message.

The output schema is simpler than command/HTTP hooks (which use
`decision`/`reason` fields in `hookSpecificOutput`).

**Use case:** Lightweight policy checks expressible as natural language rules.
Good for nuanced decisions that are hard to write as regex or shell logic.
Bad for anything requiring file access, codebase context, or deterministic
behavior.

**Trade-offs:**

- Adds LLM latency (~1-3s per hook invocation) + API token cost
- Non-deterministic — the same input may produce different decisions
- No tool access — the model can only reason about the event JSON, not
  read files or inspect the codebase

**Examples:**

```jsonc
// PreToolUse: judge if a bash command could affect production
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "prompt",
        "prompt": "Evaluate whether this Bash command could affect the production environment: $ARGUMENTS"
      }]
    }]
  }
}

// Stop: evaluate whether all tasks are actually complete before stopping
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "prompt",
        "prompt": "Evaluate if Claude should stop: $ARGUMENTS. Check if all tasks are complete."
      }]
    }]
  }
}

// PreToolUse: prevent writes to vendored directories
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "prompt",
        "prompt": "Does this file write target a vendored or generated directory (vendor/, node_modules/, generated/, dist/)? If yes, deny. Event: $ARGUMENTS"
      }]
    }]
  }
}
```

### Agent hooks (`"type": "agent"`)

Multi-step LLM evaluation with tool access. Claude Code spawns a subagent
that can use Read, Grep, and Glob to inspect the codebase before returning
a decision. The most powerful hook type — and the most expensive.

**Fields:**

| Field     | Required | Description                                                     |
| --------- | -------- | --------------------------------------------------------------- |
| `prompt`  | yes      | Prompt text. `$ARGUMENTS` is replaced with the hook input JSON. |
| `model`   | no       | Model for the subagent. Defaults to a fast model (haiku).       |
| `timeout` | no       | Timeout in seconds. Default: 60s.                               |

**How it works** (verified from binary build `ab38858`):

1. `$ARGUMENTS` substitution works the same as prompt hooks.
2. A subagent is spawned with the expanded prompt as a user message and a
   **system prompt**:

   ```
   You are verifying a stop condition in Claude Code. Your task is to
   verify that the agent completed the given plan. The conversation
   transcript is available at: <transcript_path>
   You can read this file to analyze the conversation history if needed.

   Use the available tools to inspect the codebase and verify the condition.
   Use as few steps as possible - be efficient and direct.

   When done, return your result using the StructuredOutput tool with:
   - ok: true if the condition is met
   - ok: false with reason if the condition is not met
   ```

3. The subagent has access to all tools from the main session **except**:
   TaskOutput, ExitPlanMode, EnterPlanMode, Agent, AskUserQuestion,
   TaskStop, and StructuredOutput (which is replaced by the hook's own
   `StructuredOutput` tool). The agent gets the parent's tool permission
   context set to `dontAsk` mode so it can use tools without prompting.
4. The subagent's `StructuredOutput` tool has the same `{ok, reason?}`
   schema as prompt hooks. When the agent calls it, the result is captured
   as a `structured_output` attachment and validated via Zod.
5. A nudge message is injected: `"You MUST call the StructuredOutput tool
to complete this request. Call this tool now."` (with a 5-second
   timeout) to force the agent to call the tool if it hasn't yet.
6. Max 50 tool-use turns. Default timeout: 60 seconds. Default model:
   same as prompt hooks (haiku).
7. The conversation transcript path points to the parent agent's
   transcript file, allowing the subagent to read the full conversation
   history if needed for verification.

**Use case:** Policy checks that require codebase context — verifying tests
exist for changed files, checking that edits follow style guidelines by
reading the actual style doc, confirming that imports match project
conventions.

**Trade-offs:**

- Highest latency (~5-30s depending on tool use depth) + highest token cost
  (2,000-5,000+ input tokens per invocation)
- Non-deterministic — subagent may take different tool-use paths
- The agent must call the `StructuredOutput` tool to return a decision. If
  it exhausts 50 turns or times out without calling the tool, the outcome
  is `"cancelled"` (not blocking — the action proceeds)
- The system prompt references "stop condition" and "completed the given
  plan" regardless of hook event type — this wording is hardcoded and may
  confuse the model for `PreToolUse` hooks

**Examples:**

```jsonc
// PreToolUse: verify tests exist for changed files before allowing edits
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "agent",
        "prompt": "Check whether tests exist for the changed files: $ARGUMENTS. Use Grep to search for test files matching the source file name. If no tests exist, deny with reason 'No tests found for this file'.",
        "model": "haiku"
      }]
    }]
  }
}

// Stop: verify the task is complete by reading TODO list and checking files
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "agent",
        "prompt": "Verify the task is complete: $ARGUMENTS. Read any TODO files or task descriptions. Use Grep to check that referenced files exist and contain expected changes. Allow stopping only if all items are addressed."
      }]
    }]
  }
}

// PreToolUse: enforce style guidelines by reading the actual STYLE.md
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "agent",
        "prompt": "Read STYLE.md and check if this code change follows the project's style guidelines: $ARGUMENTS. Focus on naming conventions, import ordering, and documentation requirements.",
        "model": "haiku"
      }]
    }]
  }
}
```

### Prompt vs agent: when to use which

| Criterion   | Prompt                          | Agent                                |
| ----------- | ------------------------------- | ------------------------------------ |
| Latency     | ~1-3s                           | ~5-30s                               |
| Token cost  | Low (~500 tokens)               | High (~2,000-5,000+ tokens)          |
| File access | No                              | Yes (Read, Grep, Glob)               |
| Determinism | Low                             | Low                                  |
| Best for    | Self-contained policy questions | Checks requiring codebase inspection |
| Avoid for   | Anything needing file context   | High-frequency hooks (latency)       |

### Event support by hook type

| Hook type | Supported events                                                                                                                    |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `command` | All events                                                                                                                          |
| `http`    | `PermissionRequest`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStop`, `TaskCompleted`, `UserPromptSubmit` |
| `prompt`  | Same as `http` (not supported for lifecycle events like `Setup`, `SessionStart`)                                                    |
| `agent`   | Same as `http` (not supported for lifecycle events like `Setup`, `SessionStart`)                                                    |

### Implications for daemon design (Phase 3)

HTTP hooks are a natural fit for the hook daemon architecture: the daemon
listens on `localhost:<port>`, and hooks are configured as HTTP hooks pointing
at it. This eliminates the uvx + import overhead entirely — Claude Code
makes a direct HTTP POST to the daemon.

**However**, `Setup` and `SessionStart` are command-only events. The daemon
cannot receive these via HTTP hooks. Two options:

1. **Hybrid**: Use command hooks for `Setup`/`SessionStart` (which start the
   daemon) and HTTP hooks for `PreToolUse`/`PostToolUse` (high-frequency
   events where latency matters).
2. **Command client**: Keep a thin command-hook client for all events. The
   client reads stdin, POSTs to the daemon, writes the response. This avoids
   the transport type split but still pays ~50ms command startup.

Option 1 is preferred — it uses native HTTP hooks for the hot path while
accepting command hooks for the infrequent lifecycle events.

Prompt and agent hooks are not relevant for our use case — we need custom
logic (OTEL, pre-commit, k8s secrets) that can't be expressed as LLM prompts.

## Setup Hook Invocation Path

The Setup hook is invoked by Claude Code before SessionStart, triggered by
the `environment-manager` binary during session orchestration.

### Invocation chain

```
process_api (PID 1, Rust)
    │  listens on 0.0.0.0:2024 (WebSocket)
    │  receives session parameters from Anthropic control plane
    │
    └──► environment-manager orchestrator
           │  --api-url, --service-key-file, --environment-id, ...
           │  polls /v1/environments/{env_id}/work/poll for session tasks
           │  dispatches to task-run (self-invoke or --execute-hook)
           │
           └──► environment-manager task-run --stdin --input-format=v1
                  │  parses session JSON from stdin
                  │  installs/updates Claude Code (@anthropic-ai/claude-code)
                  │  runs Manager: git clone, env config, skills, hooks bootstrap
                  │
                  ├── claude --init-only (per repo, non-fatal on failure)
                  │     │  "Running claude --init-only for session start hooks"
                  │     ├── Setup hook (trigger=init)
                  │     │     └──► our command hook (uvx claude-hook Setup ...)
                  │     │           writes to CLAUDE_ENV_FILE
                  │     └── SessionStart hook (trigger=startup), then exits
                  │
                  └── ClaudeCodeExecutor.Execute() → claude (interactive session)
                        │  the actual Claude Code session
                        └── SessionStart hook (trigger=startup)
                              └──► our command hook (uvx claude-hook SessionStart ...)
                                    writes to CLAUDE_ENV_FILE
```

### When Setup hooks fire

- **`--init`**: trigger=init, Claude Code continues to SessionStart after Setup
- **`--init-only`**: trigger=init, fires SessionStart:startup, then exits
- **`--maintenance`**: trigger=maintenance, Claude Code continues after Setup

The `task-run` subcommand uses `--init-only` during startup to trigger Setup
hooks before launching the interactive session. This is per-repo and non-fatal:
`"claude --init-only failed for repo, continuing"`. Resume modes (`resume`,
`resume-cached`) skip `--init-only` entirely for faster startup.

| Session mode    | Init script | Git clone | `claude --init-only` |
| --------------- | ----------- | --------- | -------------------- |
| `new` (default) | Yes         | Yes       | Yes                  |
| `setup-only`    | Yes         | Yes       | Yes, then exit       |
| `resume`        | Skipped     | Skipped   | Skipped              |
| `resume-cached` | Skipped     | Skipped   | Skipped              |

### Relevance to hook package installation

Setup hooks fire _before_ SessionStart. This means:

- Setup hooks cannot rely on packages installed by SessionStart (e.g., our
  hook uv environment).
- The hook command configured for Setup must be available _before_ any
  session-specific setup runs.
- For the daemon (Phase 3), the daemon cannot be started by a Setup hook
  because the daemon depends on session state (k8s secrets, proxy config)
  that SessionStart provides.
- **Practical implication**: Our hook packages must either be pre-installed
  in the container image or installed by the Setup hook itself using a
  bootstrap mechanism that doesn't depend on SessionStart's environment.

Currently, the hook uv environment is lazily created on first `uvx` invocation
(the cached wheel is pre-installed). This works because uvx handles environment
creation transparently. For the daemon approach, the daemon would be started
by SessionStart (not Setup), which is after hook packages are already available.

## Environment Variable Delivery to Hooks

Verified from the Claude Code binary (build `ab38858`). Session start hooks
can export env vars via `CLAUDE_ENV_FILE`, but those vars are **not**
automatically available to all hook types. The delivery mechanism differs by
context.

### How session env files work

1. **SessionStart/Setup hooks** receive a `CLAUDE_ENV_FILE` env var pointing
   to a writable `.sh` file (e.g.,
   `~/.claude/session-env/<id>/sessionstart-hook-0.sh`). The hook writes
   shell `export` statements to this file.
2. Claude Code caches the contents of all `sessionstart-hook-*.sh` and
   `setup-hook-*.sh` files (sorted: setup first, then sessionstart, by
   index). It also reads the file at `$CLAUDE_ENV_FILE` (if set in the
   Node.js process environment). The cache (`ro` variable in the binary)
   is a single string joining all file contents.
3. Only the **Bash tool** sources this cached script. Other hook types do
   not.

### Env availability by hook type

| Context                | `process.env` | Session env files | How                                                                                    |
| ---------------------- | ------------- | ----------------- | -------------------------------------------------------------------------------------- |
| **Bash tool**          | Yes           | **Yes**           | Session env script is prepended to command: `source <snapshot> && <env> && eval <cmd>` |
| **Command hooks**      | Yes           | **No**            | Spawned with `{...process.env, CLAUDE_PROJECT_DIR, ...}`. No `hAD()` call.             |
| **SessionStart/Setup** | Yes           | **No** (writes)   | Gets `CLAUDE_ENV_FILE` path to _write_ to, but doesn't source prior files.             |
| **HTTP hooks**         | N/A           | N/A               | HTTP POST — no subprocess, no env.                                                     |
| **Prompt hooks**       | N/A           | N/A               | LLM API call — no subprocess.                                                          |
| **Agent hooks**        | N/A           | N/A               | LLM subagent — no subprocess.                                                          |

### What command hooks DO get

The command hook subprocess environment (`W` in the binary) is constructed as:

```js
W = {
  ...process.env,             // inherit Node.js process env
  CLAUDE_PROJECT_DIR: cwd,    // project root
  // Plugin hooks only:
  CLAUDE_PLUGIN_ROOT: pluginRoot,
  CLAUDE_PLUGIN_DATA: pluginDataDir,
  CLAUDE_PLUGIN_OPTION_<KEY>: value,  // per-option from plugin config
  // SessionStart/Setup hooks only:
  CLAUDE_ENV_FILE: path,      // path to write env exports to
};
```

Key points:

- `process.env` contains what was in the parent environment when Claude
  Code started, plus any `env` entries from `settings.json` (applied via
  `Object.assign(process.env, settings.env)` at startup).
- Session env files written by earlier hooks are **not** read back into
  `process.env` — they only affect the Bash tool.

### What the Bash tool gets (for comparison)

The Bash tool constructs its command string as:

```sh
source <shell_snapshot> && <session_env_script> && <extglob_fix> && eval <command> && pwd -P >| <cwd_file>
```

Where `<session_env_script>` is the cached contents of all session env
files (the `hAD()` return value). This is why env vars exported by
SessionStart are available in Bash tool commands but not in hook commands.

### Implications for our hooks

Our `PreToolUse` and `PostToolUse` command hooks (invoked via
`uvx --from <wheel> claude-hook`) do **not** have access to env vars
exported by the SessionStart hook's `CLAUDE_ENV_FILE`. They only get
`process.env`, which includes:

- Vars set by Anthropic's container environment (e.g., `HTTPS_PROXY`)
- Vars from `settings.json` `env` blocks
- Vars set before Claude Code started

If a hook needs a var from SessionStart (e.g., `DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR`,
`BUILDBUDDY_API_KEY`), workarounds:

1. **Source the env files manually** in the hook command:

   ```json
   {
     "type": "command",
     "command": "bash -c 'for f in ~/.claude/session-env/*/sessionstart-hook-*.sh; do source \"$f\" 2>/dev/null; done; uvx --from <wheel> claude-hook'"
   }
   ```

2. **Put the vars in `settings.json` `env`** — these are applied to
   `process.env` at startup and inherited by all hooks. Only works for
   static values known before the session starts.

3. **Use the daemon architecture (Phase 3)** — the daemon is started by
   SessionStart with full env context and serves requests via HTTP.

### `CLAUDE_ENV_FILE` re-sourcing

The Bash tool re-reads session env files on every invocation (the `hAD()`
cache is invalidated by `yAD()` when env files change). This means a
background process (including the daemon) could update env vars mid-session
by writing to the env file. However, this only affects the Bash tool — not
command hooks.

## Open Questions

- **Daemon state consistency**: Since only SessionStart can populate
  `CLAUDE_ENV_FILE`, the daemon must be started during SessionStart to
  receive the correct session environment. A daemon started later (e.g.,
  by a PreToolUse hook) would lack context about the session's proxy
  config, k8s tokens, etc.
