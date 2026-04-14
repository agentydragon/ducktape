#!/usr/bin/env bash
# Laptop environment: common dev secrets only.
# Usage: eval "$(devinfra/secrets/cli_env.sh)"
#
# Does NOT export GITHUB_TOKEN or K8S_TOKEN — preserves personal tokens
# from home-manager / NixOS.
#
# Age recipients: admin + all user keys (via _common.sh secrets)
#
# Consumed by:
#   - Root .envrc (direnv)
#   - Session start hook (CLI profile)

# shellcheck source=_common.sh
source "$(dirname "$0")/_common.sh"

try_export_from_k8s DUCKTAPE_OTEL_BEARER_TOKEN claude-sandbox alloy-otlp-bearer-token token "OTEL bearer token — traces to Grafana Alloy"

# K8s service account token for claude-sandbox MCP server
try_export CLAUDE_SANDBOX_K8S_TOKEN "$REPO_ROOT/secrets/claude-web-k8s-token.yaml" '["k8s_token"]'
