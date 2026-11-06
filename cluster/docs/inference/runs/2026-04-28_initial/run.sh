#!/usr/bin/env bash
# Render the bench.py and shared-helper ConfigMap, apply the Job, tail logs, dump output.
# This is the exact command line that produced the results in README.md.
set -euo pipefail
cd "$(dirname "$0")"

NS=claude-sandbox
NAME=ollama-bench-initial

kubectl -n "$NS" delete configmap "$NAME" --ignore-not-found
kubectl -n "$NS" delete job "$NAME" --ignore-not-found

kubectl -n "$NS" create configmap "$NAME" \
  --from-file=bench.py=bench.py \
  --from-file=ollama_bench_helpers.py=../ollama_bench_helpers.py
kubectl apply -f job.yaml

kubectl -n "$NS" wait --for=condition=complete --timeout=60m "job/$NAME" &
wait_pid=$!
kubectl -n "$NS" logs -f "job/$NAME" || true
wait "$wait_pid"

kubectl -n "$NS" logs "job/$NAME" >raw_output.jsonl
echo "Wrote raw_output.jsonl"
