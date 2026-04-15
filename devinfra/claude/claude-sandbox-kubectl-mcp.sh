#!/usr/bin/env bash
# Decrypts k8s token from SOPS, assembles kubeconfig, runs kubernetes-mcp-server.
#
# Uses subprocess (not exec) so the EXIT trap cleans up the temp kubeconfig.
# SOPS_AGE_KEY must be in env — the container provides it before MCP servers start.
set -euo pipefail

TMPKC="$(mktemp "${TMPDIR:-/tmp}/claude-sandbox-kc.XXXXXX")"
trap 'rm -f "$TMPKC"' EXIT

# Canonical kubeconfig materializer: embeds CA bundle + proxy-url from the
# active profile so kubectl actually works behind the TLS-inspecting egress
# proxy on Claude Code web. See devinfra/claude/hook_daemon/write_kubeconfig_cli.py.
claude-hook write-kubeconfig "$TMPKC"

# Subprocess (not exec) — bash stays alive so the EXIT trap cleans up.
kubernetes-mcp-server --kubeconfig "$TMPKC" --disable-multi-cluster "$@"
