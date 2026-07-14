# `codex-claude`: Claude Code on ChatGPT/Codex subscription models (GPT-5.6-sol etc.) via
# the in-cluster CLIProxyAPI gateway. CLIProxyAPI speaks Anthropic /v1/messages out and the
# ChatGPT Codex backend in, translating tool calls (function_call -> tool_use) — the one
# thing LiteLLM's /v1/messages bridge and claude-code-router both couldn't do.
#
# Auth token is $CLIPROXY_CLIENT_KEY — a SOPS secret (SSOT in
# secrets/shared/cli-proxy-api-client-key.yaml) surfaced to laptops via ducktape.sopsEnv.
# CLIProxyAPI holds + auto-refreshes its OWN Codex OAuth session (separate from LiteLLM's).
#
# Gateway model discovery is on, so `/model` lists the codex slugs
# (gpt-5.4/5.5/5.6-sol/terra/luna/...). Reasoning effort is driven by Claude Code's
# `effortLevel` setting; CLIProxyAPI forwards it to Codex reasoning.effort.
{ pkgs }:
pkgs.writeShellScriptBin "codex-claude" ''
  exec env \
    IS_DEMO=1 \
    ANTHROPIC_BASE_URL=https://cli-proxy-api.allegedly.works \
    ANTHROPIC_AUTH_TOKEN="$CLIPROXY_CLIENT_KEY" \
    ANTHROPIC_API_KEY="$CLIPROXY_CLIENT_KEY" \
    ANTHROPIC_MODEL=gpt-5.6-sol \
    ANTHROPIC_DEFAULT_HAIKU_MODEL=gpt-5.6-luna \
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \
    claude "$@"
''
