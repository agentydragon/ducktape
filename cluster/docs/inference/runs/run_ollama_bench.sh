#!/usr/bin/env bash
# Called in the run directory with its Job and ConfigMap name.
set -euo pipefail

NS=claude-sandbox
NAME=${1:?expected Job name}

kubectl -n "$NS" delete configmap "$NAME" --ignore-not-found
kubectl -n "$NS" delete job "$NAME" --ignore-not-found

kubectl -n "$NS" create configmap "$NAME" \
  --from-file=bench.py=bench.py \
  --from-file=ollama_bench.py=../ollama_bench.py
kubectl apply -f job.yaml

kubectl -n "$NS" wait --for=condition=complete --timeout=60m "job/$NAME" &
wait_pid=$!
kubectl -n "$NS" logs -f "job/$NAME" || true
wait "$wait_pid"

kubectl -n "$NS" logs "job/$NAME" >raw_output.jsonl
echo "Wrote raw_output.jsonl"
