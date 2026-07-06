#!/usr/bin/env bash
# Idempotently materialize the alloy-otlp bearer and start the localhost OTLP
# forwarder (otlp_forwarder.py). Run as a profile background command on every
# claude launch (fresh container AND resume into a recycled one) — if the
# forwarder from a previous launch is still listening this is a no-op, so
# multiple invocations converge on "one forwarder, current token".
#
# Token sources, in order:
#   1. DUCKTAPE_OTEL_BEARER_TOKEN (web_env.sh SOPS decrypt — needs after_env)
#   2. the alloy-otlp-bearer Secret mirrored into this session's sandbox
#      namespace (rotator-published; see cluster/k8s/agents/authentik-jwt-rotation/)
# Neither available -> forwarder still starts and 503s until the file appears
# on a later launch; telemetry loss only, sessions unaffected.
set -uo pipefail

cache_dir="$HOME/.cache/ducktape"
token_file="$cache_dir/otel-bearer"
umask 077
mkdir -p "$cache_dir"

fetch_k8s_token() {
  # The kubeconfig is materialized by a sibling background command; retry
  # briefly so launch ordering doesn't cost us this session's token.
  for _ in $(seq 1 12); do
    if tok=$(kubectl -n "${K8S_NAMESPACE:-claude-sandbox}" get secret alloy-otlp-bearer \
      -o jsonpath='{.data.token}' 2>/dev/null | base64 -d) && [ -n "$tok" ]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

if [ -n "${DUCKTAPE_OTEL_BEARER_TOKEN:-}" ]; then
  printf '%s' "$DUCKTAPE_OTEL_BEARER_TOKEN" >"$token_file"
  echo "otel-bearer: written from DUCKTAPE_OTEL_BEARER_TOKEN"
elif command -v kubectl >/dev/null && fetch_k8s_token; then
  printf '%s' "$tok" >"$token_file"
  echo "otel-bearer: written from k8s secret alloy-otlp-bearer"
elif [ -s "$token_file" ]; then
  echo "otel-bearer: no fresh source; keeping existing $token_file"
else
  echo "otel-bearer: WARNING no source available; forwarder will 503 until $token_file exists"
fi

if (exec 3<>/dev/tcp/127.0.0.1/4318) 2>/dev/null; then
  exec 3>&- 3<&-
  echo "otlp forwarder: already listening on 4318"
  exit 0
fi

setsid nohup python3 "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}/devinfra/claude/otlp_forwarder.py" \
  >>"$cache_dir/otlp-forwarder.log" 2>&1 </dev/null &
echo "otlp forwarder: started (pid $!)"
