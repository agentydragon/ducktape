#!/usr/bin/env bash
# Web agent environment: common secrets + machine-user identity + k8s access.
# Usage: source devinfra/secrets/web_env.sh
#
# Age recipients: claude-web (+ admin via _common.sh)
#   github-pat-agentydragon-agent.yaml: admin, all user keys, claude-web, ci
#   claude-web-k8s-jwt.yaml:          admin, all user keys, claude-web
#   alloy-otlp-bearer-token.yaml:     admin, all user keys, claude-web
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

# OTEL bearer token (Grafana Alloy via Authentik) — dedicated client_credentials
# JWT rotated in-cluster and committed SOPS-encrypted to git. Session startup
# decrypts the file directly, avoiding kubectl reachability during daemon init.
# Bootstrap note: right after the TF module first creates the Authentik client
# credentials, the SOPS file may not exist until the first successful
# alloy-otlp-jwt-rotation run; that warning is expected.
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"

# Restore the caller's shell options (do not leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
