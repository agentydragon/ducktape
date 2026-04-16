#!/usr/bin/env bash
# Web agent environment: common secrets + machine-user identity + k8s access.
# Usage: source devinfra/secrets/web_env.sh
#
# Age recipients: claude-web (+ admin via _common.sh)
#   github-pat-agentydragon-agent.yaml: admin, all user keys, claude-web, ci
#   claude-web-k8s-token.yaml:          admin, claude-web
#
# Consumed by:
#   - web_setup.sh: eval'd to populate shell env, then written to
#     .claude/settings.local.json for Claude Code (MCP servers etc.)
#   - hook daemon startup_env_script: eval'd into daemon os.environ,
#     vars flow into the session env file for hook subprocesses
#
# Side effects: none beyond printing export lines. Kubeconfig is written by
# the SessionStart handler.

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Machine-user GitHub PAT (agentydragon-agent)
try_export GITHUB_TOKEN "$REPO_ROOT/secrets/github-pat-agentydragon-agent.yaml" '["github_token"]' "GitHub PAT for agentydragon-agent bot — used by gh CLI automatically. PR workflow: origin is a local proxy; PRs must come from a fork (fork remote + push/PR instructions delivered via mailbox)."

# OTEL bearer token (Grafana Alloy via Authentik) — canonical source is Authentik,
# TF writes it into this K8s Secret (cluster/terraform/gitops/alloy-otlp-bearer-token).
#
# Gated because `kubectl get secret` blocks for ~30s per retry when the k8s API
# is unreachable. Unconditional call → wedged daemon startup → dispatcher
# timeout loop → no secrets in the agent env at all. Default off until the
# api.allegedly.works:443 path has been validated end-to-end from a fresh
# web sandbox.
# CLEANUP: mirror alloy-otlp-bearer-token into SOPS and `try_export` it instead;
# see devinfra/claude/TODO.md "OTEL bearer token: mirror into SOPS".
if [ "${DUCKTAPE_ENABLE_K8S_OTEL_BEARER_TOKEN:-0}" = "1" ]; then
  try_export_from_k8s DUCKTAPE_OTEL_BEARER_TOKEN claude-sandbox alloy-otlp-bearer-token token "OTEL bearer token — traces to Grafana Alloy"
else
  echo "secrets: DUCKTAPE_OTEL_BEARER_TOKEN: skipped (DUCKTAPE_ENABLE_K8S_OTEL_BEARER_TOKEN != 1)" >&2
fi

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"
