# haku/runtime/agent — Haku's Agent Framework runtime

Runtime C from <../../plans/runtime_options.md>: an experimental
provider-agnostic, self-hosted agent loop for Haku, built on **Microsoft Agent
Framework**. It is not the primary live Haku runtime; today that is
<../claude_web_env/>. Separate component from `haku/console/` (the dashboard) —
different image, dependencies, and git write identity.

- **Model** via the in-cluster **LiteLLM** proxy (OpenAI-compatible), so the provider
  (Anthropic / OpenAI) is a LiteLLM config knob (`HAKU_MODEL`), not code.
  Only LiteLLM holds provider keys.
- **Tools**: a `run_command` shell tool (the Pod is the trust boundary — see
  <../../PLAN.md>) plus haku-console's aggregated MCP catalog (Tana reads to
  start). Bearer auth rides a pre-built `http_client` because `MCPStreamableHTTPTool`
  ignores `headers=`.
- **Behavior** is the **haku-state clone**: its root `AGENTS.md` / `SOUL.md` / `MEMORY.md`
  plus the run procedure at `memory/procedures/run.md` (the agent clones ducktape +
  haku-state at startup via pygit2; see `bootstrap.py`), read at runtime — so it stays
  single-sourced and live-editable, no image rebuild to change it. The ducktape clone is a
  source to read, not the manual.
- **Persistent threads + compaction**: history persists across restarts in
  **Valkey/Redis** via `RedisHistoryProvider` (keyed by `HAKU_SESSION_ID`) when
  `HAKU_REDIS_URL` is set — otherwise in-memory. `SummarizationStrategy` keeps the
  instruction prefix and, once history fills (`HAKU_SUMMARIZE_TARGET_COUNT` +
  `HAKU_SUMMARIZE_THRESHOLD` groups), LLM-summarizes the oldest turns into a running
  summary (using `HAKU_SUMMARIZE_MODEL` if set, else `HAKU_MODEL`) rather than dropping
  them; `HAKU_REDIS_MAX_MESSAGES` bounds the stored list.
  git (`haku-state`) remains the durable memory, so a lost cache just re-orients.
  (`agent-framework-redis` is pinned to `1.0.0b260402`, the newest beta whose core floor
  keeps `agent-framework-core` at 1.0.0.)

`main.py` (`:scan`) runs one scan — manual or scheduled. `supervisor.py` (`:serve`) is
the long-lived service: it holds one warm `AgentSession` (so the manual + run procedure
aren't re-read each wake), exposes `POST /wake` + `GET /healthz`, and self-wakes every
`HAKU_WAKE_INTERVAL_SECONDS` (0 disables). The `oci_image` and k8s wiring are the
remaining increments.

## Build / run

```bash
bbr build //haku/runtime/agent:scan
```

Config is `HAKU_*` env (see `config.py`): `HAKU_MODEL`, `HAKU_LITELLM_BASE_URL`,
`HAKU_LITELLM_API_KEY`, the clone config (`HAKU_DUCKTAPE_REPO_URL`,
`HAKU_STATE_REPO_URL`, `HAKU_GIT_HOST` / `HAKU_GIT_USERNAME` / `HAKU_GIT_PASSWORD`), and
optional `HAKU_REDIS_URL`, `HAKU_CONSOLE_TOKEN`, `HAKU_SESSION_ID`,
`HAKU_WAKE_INTERVAL_SECONDS`.

Design + tradeoffs vs. the other runtimes: <../../plans/runtime_options.md>.
