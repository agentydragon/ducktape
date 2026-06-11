@README.md

# Agent Guide for `adgn`

This file provides agent-specific conventions and prescriptions for working on the `adgn` package.

## Testing LLM Code

- Typical: `bazel test //adgn/...`
- Excluding live API tests: `bazel test //adgn/... --test_tag_filters=-live_openai_api`
- `live_openai_api` / `live_anthropic_api` tests require API keys and network access
- Tests marked `real_github` or `live_openai_api` talk to network/services; run explicitly

## Conventions

- Arg0 virtual CLIs
  - Virtual commands are exposed by argv0 name on PATH, e.g., `apply_patch` (`applypatch` alias)
    to apply OpenAI‑style patch envelopes
  - Symlink creation is strict; failures abort startup

See `mcp_infra/AGENTS.md` for MCP naming, FastMCP patterns, CallToolResult handling, compositor, resources, and subscriptions conventions.
