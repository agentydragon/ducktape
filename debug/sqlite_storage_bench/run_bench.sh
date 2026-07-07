#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
BENCH_DIR="$ROOT_DIR/debug/sqlite_storage_bench"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="$BENCH_DIR/results/$RUN_ID"
NAMESPACE=sqlite-storage-bench
STORAGE_CLASSES=(
  local-path-ovh-ssd
  local-path-ovh-hdd
  seaweedfs-ovh-ssd
  seaweedfs-ovh
)
REPEATS="${REPEATS:-5}"
PVC_SIZE="${PVC_SIZE:-8Gi}"

mkdir -p "$RESULT_DIR/manifests" "$RESULT_DIR/logs" "$RESULT_DIR/objects"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml >"$RESULT_DIR/namespace.yaml"
kubectl apply -f "$RESULT_DIR/namespace.yaml"

kubectl apply -k "$BENCH_DIR"

kubectl get storageclass "${STORAGE_CLASSES[@]}" -o yaml >"$RESULT_DIR/storageclasses.yaml"
kubectl get nodes -L topology.kubernetes.io/zone,storage.allegedly.works/tier,kubernetes.io/hostname -o wide >"$RESULT_DIR/nodes.txt"

for storage_class in "${STORAGE_CLASSES[@]}"; do
  for repeat in $(seq 1 "$REPEATS"); do
    name="sqlite-bench-${storage_class}-${repeat}"
    manifest="$RESULT_DIR/manifests/$name.yaml"
    python3 "$BENCH_DIR/render_manifests.py" \
      --storage-class "$storage_class" \
      --repeat "$repeat" \
      --run-id "$RUN_ID" \
      --pvc-size "$PVC_SIZE" >"$manifest"

    kubectl apply --dry-run=server -f "$manifest" >"$RESULT_DIR/manifests/$name.server-dry-run.yaml"
    kubectl apply -f "$manifest"
    kubectl wait --for=condition=complete "job/$name" --namespace "$NAMESPACE" --timeout=150m

    pod="$(kubectl get pods --namespace "$NAMESPACE" -l job-name="$name" -o jsonpath='{.items[0].metadata.name}')"
    kubectl logs --namespace "$NAMESPACE" "$pod" >"$RESULT_DIR/logs/$name.jsonl"
    kubectl get job,pod,pvc --namespace "$NAMESPACE" -l "sqlite-storage-bench/run-id=$RUN_ID,sqlite-storage-bench/storage-class=$storage_class,sqlite-storage-bench/repeat=$repeat" -o yaml >"$RESULT_DIR/objects/$name.yaml"
    kubectl delete job "$name" --namespace "$NAMESPACE" --wait=true
    kubectl delete pvc "$name" --namespace "$NAMESPACE" --wait=true
  done
done

python3 "$BENCH_DIR/summarize_results.py" "$RESULT_DIR" >"$RESULT_DIR/summary.md"
