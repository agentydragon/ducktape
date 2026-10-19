# haku/runtime/x/agent_framework — Haku's Agent Framework runtime

Runtime C (<../../../plans/runtime_options.md>): experimental provider-agnostic, self-hosted agent
loop on **Microsoft Agent Framework**. Not the live runtime (that is <../../claude_web_env/>).
Separate from `haku/console/`: different image, dependencies, and git write identity. Deployment
plan: <PLAN.md>.

- **Model** via in-cluster LiteLLM (OpenAI-compatible); provider is the `HAKU_MODEL` config knob.
  Only LiteLLM holds provider keys.
- **Tools**: `run_command` shell (the Pod is the trust boundary, <../../../PLAN.md>) plus
  haku-console's `/mcp` catalog when `HAKU_CONSOLE_TOKEN` is set (that endpoint is now retired in
  the console). Bearer auth rides a pre-built `http_client` because `MCPStreamableHTTPTool`
  ignores `headers=`.
- **Behavior** comes from the haku-state clone read at runtime (root `AGENTS.md` / `SOUL.md` /
  `MEMORY.md`, run procedure `memory/procedures/run.md`), so editing it needs no image rebuild.
  `bootstrap.py` clones ducktape + haku-state via pygit2; ducktape is a source to read, not the
  manual.
- **History**: `RedisHistoryProvider` on Valkey keyed by `HAKU_SESSION_ID` when `HAKU_REDIS_URL`
  is set, else in-memory; `HAKU_REDIS_MAX_MESSAGES` bounds it. `SummarizationStrategy` keeps the
  instruction prefix and, past `HAKU_SUMMARIZE_TARGET_COUNT` + `HAKU_SUMMARIZE_THRESHOLD` groups,
  summarizes the oldest turns (`HAKU_SUMMARIZE_MODEL`, else `HAKU_MODEL`). git (`haku-state`) is
  the durable memory, so a lost cache just re-orients.

`main.py` (`:scan`) runs one scan. `supervisor.py` (`:serve`) holds one warm `AgentSession`,
serves `POST /wake` + `GET /healthz`, and self-wakes every `HAKU_WAKE_INTERVAL_SECONDS` (0
disables). No `oci_image` or k8s wiring yet.

Config: `HAKU_*` env, defined in `config.py`.

```bash
bbr build //haku/runtime/x/agent_framework:scan
```
