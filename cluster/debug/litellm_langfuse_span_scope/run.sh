#!/usr/bin/env bash
# Point the scratch LiteLLM at one variant (variants/<name>/{config.yaml,env}) and wait for it.
set -euo pipefail
cd "$(dirname "$0")"
NS=scratch-langfuse-tracing
VARIANT=${1:?usage: run.sh <variant>}
DIR=variants/$VARIANT

secret() { kubectl -n "$NS" get secret scratch-secrets -o jsonpath="{.data.$1}" | base64 -d; }
PK=$(secret langfuse-public-key)
SK=$(secret langfuse-secret-key)
AUTH=$(printf '%s:%s' "$PK" "$SK" | base64 -w0)

kubectl -n "$NS" create configmap litellm-config --from-file=config.yaml="$DIR/config.yaml" \
  --dry-run=client -o yaml | kubectl apply -f -
# Both credential shapes go in every variant: the generic `otel` callback reads the OTLP
# headers, `langfuse_otel` reads the key pair; each ignores the other's.
ENV_FILE=$(mktemp)
trap 'rm -f "$ENV_FILE"' EXIT
HEADERS="Authorization=Basic $AUTH,x-langfuse-ingestion-version=4"
{
  # @OTLP_HEADERS@ lets a variant's EXPORTERS JSON carry the same headers.
  sed "s|@OTLP_HEADERS@|$HEADERS|" "$DIR/env"
  echo "OTEL_EXPORTER_OTLP_HEADERS=$HEADERS"
  echo "LANGFUSE_PUBLIC_KEY=$PK"
  echo "LANGFUSE_SECRET_KEY=$SK"
} >"$ENV_FILE"
kubectl -n "$NS" create secret generic litellm-variant-env --from-env-file="$ENV_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f litellm.yaml
kubectl -n "$NS" rollout restart deploy/litellm
kubectl -n "$NS" rollout status deploy/litellm --timeout=5m
