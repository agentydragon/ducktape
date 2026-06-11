set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: tana-claude [claude-args...]

Runs Claude Code through the Tana LiteLLM proxy.

Environment:
  TANA_LITELLM_API_KEY       LiteLLM proxy key. If unset, read from Kubernetes.
  TANA_LITELLM_BASE_URL      Proxy URL. Default: https://tana-litellm.allegedly.works
  TANA_LITELLM_MODEL         Model. Default: claude-sonnet-4-6/medium
  TANA_LITELLM_NAMESPACE     Kubernetes namespace for key lookup. Default: litellm
  TANA_LITELLM_SECRET        Kubernetes Secret for key lookup. Default: litellm-master-key
  TANA_LITELLM_SECRET_KEY    Kubernetes Secret key. Default: api-key
  CLAUDE_BIN                 Claude Code binary. Default: claude

Examples:
  tana-claude
  tana-claude --print 'Say hi.'
  TANA_LITELLM_MODEL=claude-sonnet-4-6/high tana-claude --print 'Say hi.'
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

namespace="${TANA_LITELLM_NAMESPACE:-litellm}"
secret="${TANA_LITELLM_SECRET:-litellm-master-key}"
secret_key="${TANA_LITELLM_SECRET_KEY:-api-key}"
base_url="${TANA_LITELLM_BASE_URL:-https://tana-litellm.allegedly.works}"
model="${TANA_LITELLM_MODEL:-claude-sonnet-4-6/medium}"
claude_bin="${CLAUDE_BIN:-claude}"

if [ -z "${TANA_LITELLM_API_KEY:-}" ]; then
  if ! encoded_key="$(kubectl -n "$namespace" get secret "$secret" -o "jsonpath={.data.$secret_key}" 2>&1)"; then
    echo "TANA_LITELLM_API_KEY is unset and Kubernetes Secret lookup failed:" >&2
    echo "$encoded_key" >&2
    exit 1
  fi

  if [ -z "$encoded_key" ]; then
    echo "Secret $namespace/$secret did not contain key $secret_key." >&2
    exit 1
  fi

  TANA_LITELLM_API_KEY="$(printf '%s' "$encoded_key" | base64 --decode)"
  export TANA_LITELLM_API_KEY
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

echo "Starting Claude Code via $ANTHROPIC_BASE_URL with model $ANTHROPIC_MODEL" >&2
exec "$claude_bin" "$@"
