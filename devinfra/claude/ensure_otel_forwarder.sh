#!/usr/bin/env bash
# Idempotently start the localhost OTLP forwarder (otlp_forwarder.py) and
# materialize the alloy-otlp bearer it relays with. Run as a profile background
# command on every claude launch (fresh container AND resume into a recycled
# one) — if a forwarder from a previous launch is still listening this is a
# no-op, so repeated invocations converge on "one forwarder, current token".
#
# Order matters: the forwarder starts FIRST (it serves 503s until the token
# file exists), so a slow/hung token fetch can never cost the session its
# relay — only a short 503 window.
#
# Token sources, in order:
#   1. DUCKTAPE_OTEL_BEARER_TOKEN (web_env.sh SOPS decrypt — needs after_env)
#   2. the alloy-otlp-bearer Secret mirrored into this session's sandbox
#      namespace (rotator-published; see cluster/k8s/agents/alloy-otlp-bearer/)
# Neither available -> the forwarder keeps 503ing until a later launch writes
# the file; telemetry loss only, sessions unaffected.
set -uo pipefail

: "${CLAUDE_PROJECT_DIR:?ensure_otel_forwarder.sh requires CLAUDE_PROJECT_DIR}"
cache_dir="$HOME/.cache/ducktape"
token_file="$cache_dir/otel-bearer"
umask 077
mkdir -p "$cache_dir"

if (exec 3<>/dev/tcp/127.0.0.1/4318) 2>/dev/null; then
  exec 3>&- 3<&-
  echo "otlp forwarder: already listening on 4318"
else
  # A concurrent invocation may win the bind race; the forwarder treats
  # EADDRINUSE as "already running" and exits cleanly.
  setsid nohup python3 "$CLAUDE_PROJECT_DIR/devinfra/claude/otlp_forwarder.py" \
    >>"$cache_dir/otlp-forwarder.log" 2>&1 </dev/null &
  echo "otlp forwarder: started (pid $!)"
fi

fetch_k8s_token() {
  # The kubeconfig is materialized by a sibling background command; retry
  # briefly so launch ordering doesn't cost this session its token.
  for _ in $(seq 1 10); do
    if tok=$(kubectl --request-timeout=10s -n "${K8S_NAMESPACE:-claude-sandbox}" \
      get secret alloy-otlp-bearer -o jsonpath='{.data.token}' 2>/dev/null | base64 -d) \
      && [ -n "$tok" ]; then
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
