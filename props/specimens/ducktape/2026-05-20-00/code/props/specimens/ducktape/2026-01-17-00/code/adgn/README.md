# adgn

Local tools and libraries for my dev/worktree/LLM workflows.

- Agent CLI (`adgn-agent`)
- MCP servers (Gitea mirror)
- Arg0 virtual CLI utilities
- Testing support

## Development

See the repository root AGENTS.md for the standard Bazel workflow.

```bash
bazel build //adgn/...
bazel test //adgn/...
bazel build --config=check //adgn/...  # lint + typecheck
```

### Pytest Markers

See `[tool.pytest.ini_options]` in `pyproject.toml` for markers and timeout settings.

- `live_openai_api` / `live_anthropic_api` — require API keys and network
- `real_github` — requires network access to GitHub
- Hermetic git (pytest-env): `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`

## High-Level Module Map

- Packaging: name `adgn`, Python `>=3.13`
- Agent CLI (`adgn/agent/`) — simple stdin/stdout REPL
- MCP servers (`adgn/mcp/`) — Gitea mirror server
- Tools (`adgn/tools/`) — `trivial_patterns` linter, arg0 utilities
- Testing (`adgn/testing/`) — test fixtures, bootstrap helpers
- Utilities (`adgn/util/`) — shared utilities

**Moved to separate packages:**

- Response cache → `rspcache/`
- Properties/specimens → `props/`
- Instruction optimizer → `inop/`
- System rewriter → `sysrw/`
- Seatbelt → `mcp_infra/` (under `seatbelt/`)
- MCP compositor → `mcp_infra/`

## Console scripts

See `[project.scripts]` in `pyproject.toml` for the full list of CLI entry points.

- `adgn-agent` — Agent REPL
- `adgn-trivial-patterns` — Trivial patterns linter
- `adgn-mcp-gitea-mirror` — Gitea mirror MCP server

## Agent CLI

The `adgn-agent` command provides a simple stdin/stdout REPL for running agents:

```bash
adgn-agent run                               # Start REPL with default model
adgn-agent run --model gpt-4o               # Specify model
adgn-agent run --mcp-config extra.json      # Merge additional MCP config
```

- Model/system defaults: `--model` (OPENAI_MODEL, default `gpt-5.1-codex-mini`), `--system` (SYSTEM_INSTRUCTIONS)
- MCP configuration:
  - Baseline: if present, `./.mcp.json` in CWD is always loaded
  - Repeatable: `--mcp-config /path/extra.json` merges additional configs (later overrides earlier)
  - Embedded servers: prefer Streamable HTTP (`transport: "http"`) with bearer `auth` or `headers.Authorization`
  - Compatibility: `transport: "inproc"` with `factory` is still accepted, but runs over loopback HTTP

**Note:** The Agent UI/server functionality has moved to the `agent_server/` package. See `agent_server/README.md` for web-based agent management
