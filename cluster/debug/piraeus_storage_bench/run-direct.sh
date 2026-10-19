#!/bin/sh
set -eu

namespace=piraeus-storage-bench-20261019
root=$(dirname "$0")
results="$root/results"
private_dir=${PIRAEUS_BENCH_PRIVATE_DIR:-/tmp/piraeus-storage-bench-private}
mkdir -p "$results" "$private_dir"

old_pods=$(kubectl -n "$namespace" get pods -l app.kubernetes.io/name=piraeus-storage-bench-direct -o name)
if [ -n "$old_pods" ]; then
  for old_pod in $old_pods; do
    kubectl -n "$namespace" wait --for=delete "$old_pod" --timeout=120s
  done
fi
kubectl -n "$namespace" apply -f "$root/direct-job.yaml"
kubectl -n "$namespace" wait --for=condition=Ready pod -l app.kubernetes.io/name=piraeus-storage-bench-direct --timeout=15m
pod=$(kubectl -n "$namespace" get pod -l app.kubernetes.io/name=piraeus-storage-bench-direct -o jsonpath='{.items[0].metadata.name}')
{
  for claim in repo-local-path repo-seaweedfs repo-piraeus-one repo-piraeus-two; do
    pv=$(kubectl -n "$namespace" get pvc "$claim" -o jsonpath='{.spec.volumeName}')
    handle=$(kubectl get pv "$pv" -o jsonpath='{.spec.csi.volumeHandle}')
    printf '%s	%s	%s	%s
' "$claim" "$pv" "$handle" "$(kubectl -n "$namespace" get pvc "$claim" -o jsonpath='{.status.phase}')"
  done
} >"$results/pvc-identities.tsv"

# A Pod can mount the primary while DRBD is still copying to its secondary.
# Require two healthy observations at least ten seconds apart before starting
# any filesystem workload. Every replica must be UpToDate and Established.
gate_deadline=$(($(date +%s) + 600))
stable_samples=0
while [ "$(date +%s)" -lt "$gate_deadline" ]; do
  if "$root/verify-storage.sh" piraeus-file-thin-one \
    >"$private_dir/gate-one.txt" 2>&1 \
    && "$root/verify-storage.sh" piraeus-file-thin-two-protocol-c \
      >"$private_dir/gate-two.txt" 2>&1; then
    stable_samples=$((stable_samples + 1))
    if [ "$stable_samples" -ge 2 ]; then
      break
    fi
    sleep 10
  else
    stable_samples=0
    sleep 5
  fi
done
if [ "$stable_samples" -lt 2 ]; then
  echo "Piraeus volumes did not remain healthy for two gate samples within ten minutes" >&2
  cat "$private_dir/gate-one.txt" "$private_dir/gate-two.txt" >&2
  exit 1
fi
"$root/verify-storage.sh" piraeus-file-thin-one
"$root/verify-storage.sh" piraeus-file-thin-two-protocol-c
kubectl exec -n "$namespace" "$pod" -- touch /gate/start
kubectl -n "$namespace" wait --for=condition=complete job/piraeus-storage-bench-direct --timeout=30m
kubectl -n "$namespace" logs job/piraeus-storage-bench-direct >"$results/direct-filesystem.csv"
kubectl -n "$namespace" delete job piraeus-storage-bench-direct --wait=true
