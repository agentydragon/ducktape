#!/usr/bin/env bash
# Shared helpers and common secrets for env scripts.
# Not intended to be run directly — sourced by cli_env.sh, web_env.sh, ci_env.sh.
#
# Age recipients that can decrypt secrets in this file:
#   buildbuddy.yaml: admin, all user keys, claude-web, ci
#
# On failure, writes diagnostics to stderr. Scripts export vars directly
# into the current shell — source them, don't eval their stdout.
#
# These scripts are *sourced* into the caller's shell, so they must not leak their
# strict-mode options. A leaked `set -u` in particular breaks later interactive
# commands that legitimately reference unset variables — e.g. Claude Code's
# shell-snapshot `grep`/`find` wrappers probe `[[ -n $ZSH_VERSION ]]`, which aborts
# with "ZSH_VERSION: unbound variable" once nounset is on. So we snapshot the
# caller's option state here and the entrypoint scripts restore it via
# `_secrets_restore_shell_opts` as their last line.
_secrets_saved_shell_opts="$(set +o)"
set -euo pipefail

# Restore the caller's shell options captured above (errexit/nounset/pipefail), so
# strict mode covers our own execution without persisting into the sourcing shell.
# Called at the end of each entrypoint (cli/web/ci_env). Idempotent: once the saved
# state is consumed the guard makes any further call a no-op. (The function itself is
# left defined — bash can't unset a function from within its own running body.)
_secrets_restore_shell_opts() {
  [ -n "${_secrets_saved_shell_opts:-}" ] || return 0
  eval "$_secrets_saved_shell_opts"
  unset _secrets_saved_shell_opts
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# If SOPS_AGE_KEY is not already set (web/CI inject it externally), derive it from
# the SSH key using ssh-to-age. SOPS 3.12 only auto-discovers ~/.ssh/id_rsa, not
# id_ed25519, so this is required for CLI mode on user machines.
if [ -z "${SOPS_AGE_KEY:-}" ] && command -v ssh-to-age >/dev/null 2>&1; then
  _ssh_key="${HOME}/.ssh/id_ed25519"
  if [ -f "$_ssh_key" ]; then
    if _age_key=$(ssh-to-age --private-key -i "$_ssh_key" 2>/dev/null); then
      export SOPS_AGE_KEY="$_age_key"
    fi
    unset _age_key
  fi
  unset _ssh_key
fi

# Helper: read a value from a K8s Secret via kubectl, export directly. On
# failure (no kubeconfig, no cluster access, missing secret), warns on stderr
# but continues.
try_export_from_k8s() {
  local var_name="$1" namespace="$2" secret_name="$3" key="$4" description="${5:-}"
  local _desc_suffix=""
  [ -n "$description" ] && _desc_suffix=" (${description})"
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "WARNING: secrets: $var_name${_desc_suffix}: kubectl not on PATH" >&2
    return 0
  fi
  local kubectl_stderr value
  kubectl_stderr=$(mktemp)
  if ! value=$(kubectl get secret -n "$namespace" "$secret_name" -o "jsonpath={.data.$key}" 2>"$kubectl_stderr"); then
    echo "WARNING: secrets: $var_name${_desc_suffix}: kubectl get secret failed: $(cat "$kubectl_stderr")" >&2
    rm -f "$kubectl_stderr"
    return 0
  fi
  rm -f "$kubectl_stderr"
  if [ -z "$value" ]; then
    echo "WARNING: secrets: $var_name${_desc_suffix}: empty value at ${namespace}/${secret_name}:${key}" >&2
    return 0
  fi
  if ! value=$(printf '%s' "$value" | base64 -d 2>/dev/null); then
    echo "WARNING: secrets: $var_name${_desc_suffix}: base64 decode failed" >&2
    return 0
  fi
  local _ok_msg="secrets: ${var_name}: OK"
  [ -n "$description" ] && _ok_msg="${_ok_msg} — ${description}"
  echo "$_ok_msg" >&2
  export "$var_name=$value"
}

# Helper: decrypt a SOPS file, export directly. On failure, warns on stderr
# but continues.
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
  export "$var_name=$value"
}

# --- Common secrets (shared across all contexts) ---

# BuildBuddy API key
try_export BUILDBUDDY_API_KEY "$REPO_ROOT/secrets/buildbuddy.yaml" '["buildbuddy_api_key"]' "BuildBuddy remote cache/execution (bbr)"

# CLEANUP(added 2026-06-11): The external-RBE docker-ci path is dormant. docker-ci's
# PKI moved to cert-manager (cluster-internal-ca) and the SOPS client key was
# deleted, so nothing exports DUCKTAPE_DOCKER_CLIENT_KEY and the docker_mtls
# pytest fixture no-ops (util/testing/docker_mtls.py). Reviving `bbr test`
# against docker-ci means issuing a clientAuth cert from cluster-internal-ca,
# exporting cert+key to the RBE executors, and exporting DUCKTAPE_DOCKER_CLIENT_KEY
# here (e.g. via BBR_REMOTE_ARGS secret-env-overrides). See the fixture docstring.
