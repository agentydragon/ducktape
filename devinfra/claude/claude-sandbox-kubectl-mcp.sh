#!/usr/bin/env bash
# Decrypts k8s token from SOPS, assembles kubeconfig, runs kubernetes-mcp-server.
#
# Uses subprocess (not exec) so the EXIT trap cleans up the temp kubeconfig.
# SOPS_AGE_KEY must be in env — the container provides it before MCP servers start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TMPKC="$(mktemp "${TMPDIR:-/tmp}/claude-sandbox-kc.XXXXXX")"
trap 'rm -f "$TMPKC"' EXIT

"$SCRIPT_DIR/kube_from_sops.sh" "$TMPKC"

# Subprocess (not exec) — bash stays alive so the EXIT trap cleans up.
kubernetes-mcp-server --kubeconfig "$TMPKC" --disable-multi-cluster "$@"
