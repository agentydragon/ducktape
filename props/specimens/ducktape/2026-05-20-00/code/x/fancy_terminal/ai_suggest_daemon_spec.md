# AI Autosuggest Daemon for zsh (Spec)

Implement an AI-backed autosuggestion system for zsh that integrates cleanly with `zsh-autosuggestions` via a custom strategy, backed by a local daemon that queries an LLM (OpenAI initially). The goals are low latency, zero-stall typing, safety, and easy opt-in/out.

## High-Level Requirements

- Provide command-line autosuggestions inline (gray text) via `zsh-autosuggestions` using a custom strategy `ai` that sets `typeset -g suggestion` to a full line beginning with `$BUFFER` (plugin renders only the suffix).
- Non-blocking UX: Never block typing. Suggestions appear opportunistically under latency budgets.
- Robustness: One daemon per user; lazy-start and self-heal on crash; secure socket permissions.
- Safety: Strict prefix-only completions, short timeouts, simple redaction of recent history.
- Configurable: Env vars to enable/disable, set model, tokens, timeouts, debounce, cache.

## Components

1. zsh client integration (boundary-only, not a tutorial)

- Implement `_zsh_autosuggest_strategy_ai` per zsh-autosuggestions’ strategy contract:
  - Input: called with `$1=$BUFFER` (current line/prefix).
  - Output: set `typeset -g suggestion` to the full suggested line (must start with `$1`), or leave it unset if no suggestion.
- Guardrails: check `AI_AUTOSUGGEST` and deps; skip if `${#BUFFER} < $AI_SUGGEST_MIN_PREFIX_LEN` or `$NO_AI_SUGGEST`.
- Daemon use: ensure daemon is healthy once per shell; send `suggest` (cwd, buffer, recent) over the UNIX socket; enforce client min-interval; return immediately (non-blocking).

2. Daemon process (Python 3.11+, `asyncio`)

- Single-process UNIX domain socket server at `$AI_SUGGEST_SOCK` (default: `~/.cache/ai_suggest/daemon.sock`).
- Protocol: NDJSON (one JSON object per line), `v:1` on all messages.
- Requests:
  - `type: "ping"` → respond with `{ok:true, version, pid}`
  - `type: "suggest"` with fields:
    - `model: str` (e.g., `gpt-4o-mini`)
    - `cwd: str` (absolute path)
    - `buffer: str`
    - `recent: list[str]` (optional)
    - `require_prefix_match: bool` (default true)
    - `min_prefix_len: int` (default 4)
    - `timeout_ms: int` (default 1000)
    - `max_tokens: int` (default 64)
  - Server behavior:
    - Debounce/coalesce by `(cwd, buffer_prefix)` so rapid changes don’t flood the backend.
    - Enforce per-client rate limit (>=1200 ms between backend calls).
    - LRU cache keyed on `(model, cwd, prefix_hash, recent_hash)` with TTL (default 300s).
    - Redact trivial secrets from `recent` (emails, tokens-ish patterns, URLs with query) before sending to LLM.
    - Compose a minimal prompt: cwd, recent (truncated), buffer, and a hard instruction to return exactly one zsh command that begins with `buffer`; otherwise return nothing.
    - Timeout after `timeout_ms` and return `ok:true, reason:"no_suggestion"`.
    - On success, return `{ok:true, suggestion, suffix, latency_ms, cache_hit, ttl_ms}` with `suffix = suggestion[len(buffer):]`.
    - On error, return `{ok:false, code, message}`.

3. OpenAI adapter

- Simple class `OpenAIAdapter` with `complete(buffer, context) -> str|None`.
- Use `openai` Python SDK or shell out to `openai api chat.completions.create`.
- Model configurable via `AI_SUGGEST_MODEL` (default: `gpt-4o-mini`).
- Cap tokens (64), temperature low (0.2–0.3), and include a strict system prompt to enforce prefix completion.

4. Spawn/health management (client-side)

- Function `ensure_ai_daemon_running` in zsh:
  - Socket dir `~/.cache/ai_suggest` 0700; PID/log files there.
  - If socket exists: attempt `ping` with 200 ms timeout; if ok, return.
  - Lock dir to serialize startup; if held, wait up to 500 ms for ping; if still down, treat as stale and attempt a clean restart.
  - Start daemon: `(setsid nohup python3 -m ai_suggest.daemon >"$LOG" 2>&1 & echo $! >"$PID") </dev/null`
  - Poll ping up to ~10x50 ms; on success, done; else kill PID, remove socket, release lock, return (no-suggest this time).

5. CLI helpers (optional)

- `ai-suggest status|stop|restart` shell functions for manual control.

## Environment Variables (defaults)

- `AI_AUTOSUGGEST=1` (opt-in) — enable zsh strategy
- `AI_SUGGEST_SOCK=~/.cache/ai_suggest/daemon.sock`
- `AI_SUGGEST_MODEL=gpt-4o-mini`
- `AI_SUGGEST_TIMEOUT_MS=1000`
- `AI_SUGGEST_DEBOUNCE_MS=350`
- `AI_SUGGEST_MIN_PREFIX_LEN=4`
- `AI_SUGGEST_MAX_HISTORY=5`
- `AI_SUGGEST_CACHE_TTL_MS=300000`
- `AI_SUGGEST_MAX_TOKENS=64`

## zsh ↔ zsh-autosuggestions boundary (exact API)

- Strategy function naming: `_zsh_autosuggest_strategy_<name>`
- Call signature: called with one positional arg `$1` = current `$BUFFER` (prefix).
- Provider contract:
  - Set a global variable `suggestion` (e.g., `typeset -g suggestion=...`) with a full line that must begin with `$1`.
  - If no valid suggestion, do not set `suggestion` (or unset it). The core will ignore non-matching values.
- Rendering by plugin: it sets `POSTDISPLAY="${suggestion#$BUFFER}"` and highlights via `region_highlight` using `ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE`.
- Acceptance widgets exposed by plugin: `autosuggest-accept`, `autosuggest-partial-accept`, `autosuggest-execute`, `autosuggest-clear`, etc. You may `bindkey` these; your strategy does not handle acceptance.
- Strategy selection: `ZSH_AUTOSUGGEST_STRATEGY=(ai history completion ...)` — first provider setting a valid `suggestion` wins.
- Async behavior: with `ZSH_AUTOSUGGEST_USE_ASYNC=1`, the plugin fetches suggestions off-thread and then updates `POSTDISPLAY` on arrival.

## Socket Protocol (NDJSON)

- Request example:

```json
{
  "v": 1,
  "id": "42",
  "type": "suggest",
  "model": "gpt-4o-mini",
  "cwd": "/Users/rai/code/openai",
  "buffer": "pytest -k ",
  "recent": ["rg TODO", "git status"],
  "require_prefix_match": true,
  "timeout_ms": 900
}
```

- Success response:

```json
{
  "v": 1,
  "id": "42",
  "ok": true,
  "suggestion": "pytest -k my_test -q",
  "suffix": "my_test -q",
  "latency_ms": 180,
  "cache_hit": false,
  "ttl_ms": 300000
}
```

- No suggestion:

```json
{
  "v": 1,
  "id": "42",
  "ok": true,
  "suggestion": "",
  "suffix": "",
  "reason": "no_suggestion",
  "latency_ms": 120
}
```

- Error:

```json
{ "v": 1, "id": "42", "ok": false, "code": "timeout", "message": "backend timed out" }
```

## Prompting Guidance

- System: "You are a shell assistant. Output ONE zsh command only, no explanations. The output MUST begin with the exact `Current buffer` string and be a completion of it. If you cannot complete safely, output nothing."
- User: include `PWD`, last N redacted commands, and `Current buffer:` followed by the buffer.

## Performance Targets

- P95 suggestion latency (cold): ≤ 1000 ms
- P95 suggestion latency (cached): ≤ 200 ms
- zsh strategy path budget: ≤ 10 ms (excluding I/O timeouts)

## Deliverables

- Python package `ai_suggest` with modules:
  - `ai_suggest/daemon.py` (asyncio server, NDJSON, cache/rate-limit/debounce)
  - `ai_suggest/backends/openai_backend.py`
  - `ai_suggest/utils.py` (redaction, hashing, LRU)
- Minimal installer/runner: `python3 -m ai_suggest.daemon`
- zsh snippet:
  - `_zsh_autosuggest_strategy_ai` + `ensure_ai_daemon_running` + env config
  - Does not block; strategy sets `typeset -g suggestion` (full line starting with `$BUFFER`); plugin renders suffix via `POSTDISPLAY`
- README with setup & troubleshooting; tests for the protocol and backend adapter.

## Nice-to-haves (later)

- Ollama/Gemini backends via adapter interface
- launchd unit (macOS) / systemd user unit (Linux)
- Telemetry OFF by default; opt-in simple counters to file for perf debugging
- Optional allowlist of commands/prefixes
