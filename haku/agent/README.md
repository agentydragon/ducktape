# haku/agent — Haku's Agent Framework runtime

Runtime C from <../plans/runtime_options.md>: a provider-agnostic, self-hosted agent
loop for Haku, built on **Microsoft Agent Framework**. Separate component from
`haku/console/` (the dashboard) — different image, dependencies, and git write
identity.

- **Model** via the in-cluster **LiteLLM** proxy (OpenAI-compatible), so the provider
  (Anthropic / OpenAI / Z.AI-GLM) is a LiteLLM config knob (`HAKU_MODEL`), not code.
  Only LiteLLM holds provider keys.
- **Tools**: a `run_command` shell tool (the Pod is the trust boundary — see
  <../PLAN.md>) plus remote MCP toolsets (Tana to start). Bearer auth rides a
  pre-built `http_client` because `MCPStreamableHTTPTool` ignores `headers=`.
- **Behavior** is the baked `haku/base/` manual + `haku/run.md`, read at runtime — not
  inlined — so it stays single-sourced in ducktape and reconciled per run.
- **Compaction**: `SlidingWindowStrategy` preserves the instruction prefix and bounds
  the history, so re-reads reuse the cached prefix instead of re-processing the manual
  cold. **Cross-restart persistence is the next increment** — Agent Framework (pinned
  at 1.0.0 here) ships no prebuilt Postgres history provider; the prebuilt options are
  `agent-framework-redis` and the in-core `FileHistoryProvider` (not exported in 1.0.0).
  Today the scan runs stateless and re-orients from `haku-state` each run (git is the
  durable memory); the persistence backend is a pending choice.

`main.py` (`:scan`) runs one scan — manual or scheduled. `supervisor.py` (`:serve`) is
the long-lived service: it holds one warm `AgentSession` (so the manual + run procedure
aren't re-read each wake), exposes `POST /wake` + `GET /healthz`, and self-wakes every
`HAKU_WAKE_INTERVAL_SECONDS` (0 disables). The `oci_image` and k8s wiring are the
remaining increments.

## Build / run

```bash
bbr build //haku/agent:scan
```

Config is `HAKU_*` env (see `config.py`): `HAKU_MODEL`, `HAKU_LITELLM_BASE_URL`,
`HAKU_LITELLM_API_KEY`, optional `HAKU_TANA_RO_TOKEN`, `HAKU_SESSION_ID`,
`HAKU_WAKE_INTERVAL_SECONDS`, `HAKU_STATE_DIR`, `HAKU_BASE_DIR`.

Design + tradeoffs vs. the other runtimes: <../plans/runtime_options.md>.
