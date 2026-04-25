#!/usr/bin/env bash
# Decrypts the SOPS-encrypted k8s JWT, assembles a kubeconfig, and runs
# kubernetes-mcp-server against it. See devinfra/claude/scripts/write_kubeconfig.py
# for the token-auth rationale (TL;DR: Anthropic's egress proxy is an L7 MITM
# that eats client certs, so we carry auth in the Authorization header).
#
# Uses subprocess (not exec) so the EXIT trap cleans up the temp kubeconfig.
set -euo pipefail

# Claude Code doesn't pass CLAUDE_PROJECT_DIR or direnv exports to MCP server
# subprocesses. Derive both from this script's location (repo_root/devinfra/claude/).
if [[ -z "${CLAUDE_PROJECT_DIR:-}" ]]; then
  CLAUDE_PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
  export CLAUDE_PROJECT_DIR
fi

# SOPS_AGE_KEY: on CLI, derive from the user's SSH key (same as cli_env.sh).
# On web, the hook daemon sets it via startup_env_script.
if [[ -z "${SOPS_AGE_KEY:-}" ]] && command -v ssh-to-age &>/dev/null; then
  SOPS_AGE_KEY="$(ssh-to-age --private-key -i ~/.ssh/id_ed25519 2>/dev/null)" || true
  export SOPS_AGE_KEY
fi

# mktemp atomically creates a 0-byte file with mode 0600 (no TOCTOU); write_kubeconfig.py
# tolerates an empty existing file and overwrites it via tempfile + atomic rename.
TMPKC="$(mktemp "${TMPDIR:-/tmp}/claude-sandbox-kc.XXXXXX")"
trap 'rm -f "$TMPKC"' EXIT

python3 "$CLAUDE_PROJECT_DIR/devinfra/claude/scripts/write_kubeconfig.py" "$TMPKC"

# Subprocess (not exec) — bash stays alive so the EXIT trap cleans up.
kubernetes-mcp-server --kubeconfig "$TMPKC" --disable-multi-cluster "$@"
