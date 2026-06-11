#!/usr/bin/env sh
set -eu

usage() {
  cat >&2 <<'EOF'
Usage: run_claude_code.sh [model] [claude-args...]

Runs Claude Code through the Tana LiteLLM proxy.

Environment:
  TANA_LITELLM_API_KEY       LiteLLM proxy key. If unset, read from Kubernetes.
  TANA_LITELLM_BASE_URL      Proxy URL. Default: https://tana-litellm.allegedly.works
  TANA_LITELLM_MODEL         Model if no positional model is given.
                             Default: claude-sonnet-4-20250514
  TANA_LITELLM_NAMESPACE     Kubernetes namespace for key lookup. Default: litellm
  TANA_LITELLM_SECRET        Kubernetes Secret for key lookup. Default: litellm-master-key
  TANA_LITELLM_SECRET_KEY    Kubernetes Secret key. Default: api-key
  TANA_LITELLM_PORT_FORWARD  Set to 1 to use kubectl port-forward instead of public DNS.
  TANA_LITELLM_LOCAL_PORT    Local port for port-forward. Default: 4000
  CLAUDE_BIN                 Claude Code binary. Default: claude

Examples:
  ./run_claude_code.sh
  ./run_claude_code.sh --bare --print 'Say hi.'
  ./run_claude_code.sh claude-sonnet-4-20250514 --dangerously-skip-permissions
  TANA_LITELLM_PORT_FORWARD=1 ./run_claude_code.sh
EOF
}

decode_base64() {
  if printf '' | base64 -d >/dev/null 2>&1; then
    base64 -d
  else
    base64 -D
  fi
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

namespace="${TANA_LITELLM_NAMESPACE:-litellm}"
secret="${TANA_LITELLM_SECRET:-litellm-master-key}"
secret_key="${TANA_LITELLM_SECRET_KEY:-api-key}"
base_url="${TANA_LITELLM_BASE_URL:-https://tana-litellm.allegedly.works}"
model="${TANA_LITELLM_MODEL:-claude-sonnet-4-20250514}"
claude_bin="${CLAUDE_BIN:-claude}"

if [ "${1:-}" != "" ] && [ "${1#-}" = "$1" ]; then
  model="$1"
  shift
fi

if [ -z "${TANA_LITELLM_API_KEY:-}" ]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "TANA_LITELLM_API_KEY is unset and kubectl is not on PATH." >&2
    exit 1
  fi

  encoded_key="$(kubectl -n "$namespace" get secret "$secret" -o "jsonpath={.data.${secret_key}}")"
  if [ -z "$encoded_key" ]; then
    echo "Secret ${namespace}/${secret} did not contain key ${secret_key}." >&2
    exit 1
  fi

  TANA_LITELLM_API_KEY="$(printf '%s' "$encoded_key" | decode_base64)"
  export TANA_LITELLM_API_KEY
fi

if [ "${TANA_LITELLM_PORT_FORWARD:-0}" = "1" ]; then
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "TANA_LITELLM_PORT_FORWARD=1 requires kubectl on PATH." >&2
    exit 1
  fi

  local_port="${TANA_LITELLM_LOCAL_PORT:-4000}"
  port_forward_log="${TMPDIR:-/tmp}/tana-litellm-port-forward.$$.log"
  kubectl -n "$namespace" port-forward svc/tana-litellm "${local_port}:4000" >"$port_forward_log" 2>&1 &
  port_forward_pid="$!"

  cleanup() {
    kill "$port_forward_pid" >/dev/null 2>&1 || true
    rm -f "$port_forward_log"
  }
  trap cleanup EXIT INT TERM

  sleep 2
  if ! kill -0 "$port_forward_pid" >/dev/null 2>&1; then
    echo "kubectl port-forward failed:" >&2
    cat "$port_forward_log" >&2
    exit 1
  fi

  base_url="http://127.0.0.1:${local_port}"
fi

if ! command -v "$claude_bin" >/dev/null 2>&1; then
  echo "Claude Code binary not found: $claude_bin" >&2
  exit 1
fi

export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$base_url}"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$TANA_LITELLM_API_KEY}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$TANA_LITELLM_API_KEY}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$model}"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="${CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY:-1}"

echo "Starting Claude Code via ${ANTHROPIC_BASE_URL} with model ${ANTHROPIC_MODEL}" >&2
if [ "${TANA_LITELLM_PORT_FORWARD:-0}" = "1" ]; then
  "$claude_bin" "$@"
  exit $?
fi

exec "$claude_bin" "$@"
