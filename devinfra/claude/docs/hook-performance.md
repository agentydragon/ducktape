# Hook Performance: Reducing Latency When External Services Are Down

## Problem

Every Claude Code hook invocation (PreToolUse, PostToolUse) goes through
`hook_dispatch.py`, which initializes OTEL tracing and flushes spans on exit.
When the cluster is down (OTEL endpoint unreachable, k8s API unresponsive),
each hook call blocks for up to ~5.7s — making sessions painfully slow.

### Measured Latency Breakdown

| Component                                    | Latency          | Notes                                         |
| -------------------------------------------- | ---------------- | --------------------------------------------- |
| Bare python startup                          | ~14ms            | Baseline                                      |
| uvx resolution (cached wheel)                | ~200ms           | uv environment lookup + symlink setup         |
| Hook imports (pydantic, opentelemetry, etc.) | ~400ms           | Module-level imports in `hook_dispatch.py`    |
| **Full hook invocation (happy path)**        | **~600-700ms**   | uvx + imports + dispatch + handler            |
| OTEL `force_flush` (endpoint unreachable)    | **up to 5000ms** | `hook_dispatch.py:104`, `timeout_millis=5000` |
| **Full hook invocation (cluster down)**      | **~5700ms**      | The actual problem                            |

The ~200ms uvx delta (vs direct python) is the cost of uv resolving the cached
environment. The ~400ms import cost is pydantic + opentelemetry SDK. The 5s OTEL
flush is the dominant problem.

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

## Alternatives

### A: Quick Fix — Skip OTEL for Frequent Hooks

**Change**: In `hook_dispatch.py`, only initialize OTEL for `SessionStart`.
Skip `init_from_config` and `force_flush` for `PreToolUse`/`PostToolUse`.

```python
# hook_dispatch.py, simplified
should_trace = isinstance(parsed, SessionStartHookInput)
if should_trace and config and config.otel and config.otel.endpoint:
    otel.init_from_config(config.otel)
# ...
finally:
    if should_trace:
        provider.force_flush(timeout_millis=2000)
```

Also reduce SessionStart flush timeout from 5000ms to 2000ms.

**Pros:**

- ~30 minutes to implement. 3 files, <20 lines changed.
- Per-hook latency drops from ~5700ms to ~700ms (uvx + imports) when
  cluster is down. No regression when cluster is up.
- No new infrastructure. No new failure modes.

**Cons:**

- Still pays ~700ms per hook (uvx + imports).
- No circuit breaker — if OTEL breaks during SessionStart, it still
  blocks for 2s there.
- Does not help if we later add hooks that need external services.

**Latency when cluster down:** ~700ms per PreToolUse/PostToolUse, ~2700ms for
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

### D: Hybrid Phased Approach (A → B → C)

**Phase 1** (immediate, ~1 hour): Implement Alternative A.

- Skip OTEL for PreToolUse/PostToolUse.
- Reduce SessionStart flush timeout to 2000ms.
- Immediate relief for the acute problem.

**Phase 2** (near-term, ~1 day): Add file-based state cache (B).

- SessionStart writes `hook_state.json`.
- Circuit breaker for OTEL health.
- Design state file format to be reusable as daemon backing store.

**Phase 3** (future, ~3-5 days): Daemon on TCP socket (C).

- Daemon reads `hook_state.json` on startup as initial state.
- Maintains in-memory state, periodically persists.
- Client replaces uvx invocation.
- Full latency savings realized.

**Each phase is independently valuable and shippable.** If Phase 3 never
ships, Phases 1+2 still eliminate the acute problem and provide circuit
breaker behavior.

## Comparison Matrix

| Criterion                       | A: Quick Fix | B: State Cache        | C: Daemon        | D: Hybrid       |
| ------------------------------- | ------------ | --------------------- | ---------------- | --------------- |
| Per-hook latency (cluster down) | ~700ms       | ~700ms                | ~50ms            | 50ms (after P3) |
| Per-hook latency (cluster up)   | ~700ms       | ~700ms                | ~50ms            | 50ms (after P3) |
| SessionStart overhead change    | -3s          | -3s + circuit breaker | -5s              | Progressive     |
| Implementation effort           | ~1h          | ~1d                   | ~3-5d            | Progressive     |
| New failure modes               | None         | Stale state file      | Daemon lifecycle | Progressive     |
| gVisor compatibility            | N/A          | File I/O only         | TCP (proven)     | All proven      |
| Backwards compatible            | Yes          | Yes                   | Needs new client | Yes per phase   |

## Recommendation

**Alternative D (Hybrid Phased Approach).**

Phase 1 solves the acute pain (5s OTEL timeout on every hook) in ~1 hour
with zero risk. The PreToolUse/PostToolUse spans are low-value telemetry
— losing them costs nothing.

Phase 2 adds circuit breaker durability so SessionStart also degrades
gracefully on repeat failures.

Phase 3 is the right long-term architecture for eliminating the ~700ms
baseline, but is only justified when the per-hook latency budget matters
enough to warrant the complexity.

## Open Questions

- **`Setup` hook**: Claude Code's binary contains a `Setup` schema alongside
  the hook schemas. Its semantics and availability are not fully documented.
  If Setup hooks become available, they could replace some SessionStart
  responsibilities (one-time machine setup vs per-session init).

- **`CLAUDE_ENV_FILE` re-sourcing**: Confirmed that Claude Code re-sources
  the env file on every Bash tool call. This means a background process
  (including the daemon) could update env vars mid-session by writing to
  the env file. However, only SessionStart hooks receive `CLAUDE_ENV_FILE`
  in their environment — other hooks and the daemon would need the path
  passed to them explicitly (e.g., via supervisor environment).

- **Daemon state consistency**: Since only SessionStart can populate
  `CLAUDE_ENV_FILE`, the daemon must be started during SessionStart to
  receive the correct session environment. A daemon started later (e.g.,
  by a PreToolUse hook) would lack context about the session's proxy
  config, k8s tokens, etc.
