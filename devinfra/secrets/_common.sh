#!/usr/bin/env bash
# Shared helpers and common secrets for env scripts.
# Not intended to be run directly — sourced by cli_env.sh, web_env.sh, ci_env.sh.
#
# Age recipients that can decrypt secrets in this file:
#   buildbuddy.yaml:               admin, all user keys, claude-web, ci
#   docker-ci/client-key.sops.pem: admin, claude-web, ci
#
# On failure, writes diagnostics to stderr. Stdout contains only valid
# export lines — safe to eval even on partial failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Helper: decrypt a SOPS file, output an export line. On failure, warns on stderr
# but continues (stdout only contains successful exports, safe to eval).
try_export() {
  local var_name="$1" file="$2" extract="${3:-}" description="${4:-}"
  if [ ! -f "$file" ]; then
    echo "WARNING: secrets: $var_name: file not found: $file" >&2
    return 0
  fi
  local value sops_stderr
  sops_stderr=$(mktemp)
  local sops_args=(-d)
  [ -n "$extract" ] && sops_args+=(--extract "$extract")
  sops_args+=("$file")
  if ! value=$(sops "${sops_args[@]}" 2>"$sops_stderr"); then
    local _desc_suffix=""
    [ -n "$description" ] && _desc_suffix=" (${description})"
    echo "WARNING: secrets: $var_name${_desc_suffix}: sops decrypt failed: $(cat "$sops_stderr")" >&2
    rm -f "$sops_stderr"
    return 0
  fi
  rm -f "$sops_stderr"
  local _ok_msg="secrets: ${var_name}: OK"
  [ -n "$description" ] && _ok_msg="${_ok_msg} — ${description}"
  echo "$_ok_msg" >&2
  printf 'export %s=%q\n' "$var_name" "$value"
}

# --- Common secrets (shared across all contexts) ---

# BuildBuddy API key
try_export BUILDBUDDY_API_KEY "$REPO_ROOT/secrets/buildbuddy.yaml" '["buildbuddy_api_key"]' "BuildBuddy remote cache/execution (bbr)"

# TODO: Once docker-ci is working in cluster, put the decrypted PEM into
# BBR_REMOTE_ARGS as:
#   --remote_run_header=x-buildbuddy-platform.secret-env-overrides-base64=DUCKTAPE_DOCKER_CLIENT_KEY=<base64>
# bb remote handles the base64 natively so no manual pre-encoding needed.
# Update docker_mtls.py to read the raw PEM instead of base64-decoding.
# Docker CI mTLS — only the base64-encoded client key.
# Python code (docker_mtls fixture) assembles DOCKER_HOST / DOCKER_TLS_VERIFY /
# DOCKER_CERT_PATH atomically from this single value.
# _dk_file="$REPO_ROOT/secrets/docker-ci/client-key.sops.pem"
# if [ -f "$_dk_file" ]; then
#   _dk_stderr=$(mktemp)
#   if _dk=$(sops -d "$_dk_file" 2>"$_dk_stderr"); then
#     _dk_b64=$(printf '%s' "$_dk" | base64 -w0)
#     printf 'export DUCKTAPE_DOCKER_CLIENT_KEY=%s\n' "$_dk_b64"
#   else
#     echo "WARNING: secrets: DUCKTAPE_DOCKER_CLIENT_KEY: sops decrypt failed: $(cat "$_dk_stderr")" >&2
#   fi
#   rm -f "$_dk_stderr"
# else
#   echo "WARNING: secrets: DUCKTAPE_DOCKER_CLIENT_KEY: file not found: $_dk_file" >&2
# fi
