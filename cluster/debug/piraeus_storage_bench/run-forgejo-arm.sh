#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 TARGET_NAME DATABASE_NAME PVC_NAME" >&2
  exit 2
fi

target="$1"
database="$2"
pvc="$3"
namespace=piraeus-storage-bench-20261019
chart=oci://code.forgejo.org/forgejo-helm/forgejo
version=17.1.1
private_dir=${PIRAEUS_BENCH_PRIVATE_DIR:-/tmp/piraeus-storage-bench-private}
results="$(dirname "$0")/results"
runtime_file="$results/forgejo-runtime.tsv"
gate_file="$results/forgejo-storage-gates.tsv"
mkdir -p "$results"
case "$database" in
  bench_local_path) database_object=piraeus-bench-local-path ;;
  bench_seaweedfs) database_object=piraeus-bench-seaweedfs ;;
  bench_file_thin_one) database_object=piraeus-bench-file-thin-one ;;
  bench_file_thin_two) database_object=piraeus-bench-file-thin-two ;;
  *)
    echo "unknown benchmark database: $database" >&2
    exit 2
    ;;
esac

mkdir -p /tmp/piraeus-bench-helm/cache /tmp/piraeus-bench-helm/config /tmp/piraeus-bench-helm/data
kubectl -n "$namespace" wait --for=condition=Ready cluster/piraeus-storage-bench-db --timeout=15m
kubectl -n "$namespace" rollout status deployment/piraeus-storage-bench-valkey --timeout=10m
if [ -f "$runtime_file" ] && [ "$(wc -l <"$runtime_file")" -gt 1 ]; then
  if kubectl -n "$namespace" get deployment forgejo-bench >/dev/null 2>&1; then
    kubectl -n "$namespace" scale deployment/forgejo-bench --replicas=0
  fi
  old_pods=$(kubectl -n "$namespace" get pods \
    -l app.kubernetes.io/instance=forgejo-bench -o name)
  if [ -n "$old_pods" ]; then
    kubectl -n "$namespace" wait --for=delete $old_pods --timeout=120s
  fi
  before=$(kubectl -n "$namespace" exec deploy/piraeus-storage-bench-valkey -- valkey-cli -n 0 DBSIZE)
  flush_result=$(kubectl -n "$namespace" exec deploy/piraeus-storage-bench-valkey -- valkey-cli -n 0 FLUSHDB)
  after=$(kubectl -n "$namespace" exec deploy/piraeus-storage-bench-valkey -- valkey-cli -n 0 DBSIZE)
  if [ "$flush_result" != OK ] || [ "$after" != 0 ]; then
    echo "failed to clear only disposable Valkey DB 0 between Forgejo cells" >&2
    exit 1
  fi
  reset_file="$results/forgejo-valkey-resets.tsv"
  if [ ! -f "$reset_file" ]; then
    printf 'before_target\tflushed_at_utc\tdb\tkeys_before\tflush_result\tkeys_after\n' >"$reset_file"
  fi
  printf '%s\t%s\t0\t%s\t%s\t%s\n' "$target" "$(date -u +%FT%TZ)" \
    "$before" "$flush_result" "$after" >>"$reset_file"
fi
kubectl -n "$namespace" wait --for=jsonpath='{.status.applied}'=true database/"$database_object" --timeout=15m
# `env` keeps Helm's cache writable without changing the developer's Nix/Helm state.
direnv exec . env \
  HELM_CACHE_HOME=/tmp/piraeus-bench-helm/cache \
  HELM_CONFIG_HOME=/tmp/piraeus-bench-helm/config \
  HELM_DATA_HOME=/tmp/piraeus-bench-helm/data \
  helm upgrade --install forgejo-bench "$chart" \
  --version "$version" \
  --namespace "$namespace" \
  --values "$(dirname "$0")/forgejo-values.yaml" \
  --set-string "gitea.config.database.NAME=$database" \
  --set-string "persistence.claimName=$pvc" \
  --wait --timeout 15m

kubectl -n "$namespace" rollout status deployment/forgejo-bench --timeout=15m
if [ ! -f "$runtime_file" ]; then
  printf 'target\tcomponent\tnode\timage\timage_id\tpvc_expected\tpvc_actual\n' >"$runtime_file"
fi
app_pod=$(kubectl -n "$namespace" get pods \
  -l app.kubernetes.io/instance=forgejo-bench -o json \
  | jq -er '[.items[] | select(.status.phase == "Running")] | if length == 1 then .[0].metadata.name else error("expected one running Forgejo pod") end')
app_pod_json=$(kubectl -n "$namespace" get pod "$app_pod" -o json)
app_node=$(printf '%s' "$app_pod_json" | jq -r '.spec.nodeName')
app_pvc=$(printf '%s' "$app_pod_json" | jq -er '[.spec.volumes[] | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName] | if length == 1 then .[0] else error("expected exactly one application PVC") end')
if [ "$app_node" != ovh-ns102453 ] || [ "$app_pvc" != "$pvc" ]; then
  echo "Forgejo Pod landed on $app_node with PVC $app_pvc; expected ovh-ns102453 and $pvc" >&2
  exit 1
fi
printf '%s' "$app_pod_json" | jq -r --arg target "$target" --arg expected_pvc "$pvc" '
  . as $pod | .status.containerStatuses[] | select(.name == "forgejo") |
  [$target, "forgejo", $pod.spec.nodeName, .image, .imageID, $expected_pvc,
    ($pod.spec.volumes[] | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName)] | @tsv
' >>"$runtime_file"
case "$target" in
  piraeus-file-thin-one | piraeus-file-thin-two-protocol-c)
    gate_log="$private_dir/forgejo-gate-$target.txt"
    gate_deadline=$(($(date +%s) + 600))
    stable_samples=0
    while [ "$(date +%s)" -lt "$gate_deadline" ]; do
      if "$(dirname "$0")/verify-storage.sh" "$target" >"$gate_log" 2>&1; then
        sample_time=$(date -u +%FT%TZ)
        if [ "$stable_samples" -eq 0 ]; then
          first_sample=$sample_time
        fi
        stable_samples=$((stable_samples + 1))
        if [ "$stable_samples" -ge 2 ]; then
          if [ ! -f "$gate_file" ]; then
            printf 'target\tfirst_healthy_at_utc\tsecond_healthy_at_utc\n' >"$gate_file"
          fi
          printf '%s\t%s\t%s\n' "$target" "$first_sample" "$sample_time" \
            >>"$gate_file"
          break
        fi
        sleep 10
      else
        stable_samples=0
        sleep 5
      fi
    done
    if [ "$stable_samples" -lt 2 ]; then
      echo "$target did not remain healthy for two gate samples within ten minutes" >&2
      cat "$gate_log" >&2
      exit 1
    fi
    "$(dirname "$0")/verify-storage.sh" "$target"
    ;;
esac

rendered="$private_dir/forgejo-job.yaml"
sed "s/REPLACE_TARGET_NAME/$target/" "$(dirname "$0")/forgejo-job.yaml" >"$rendered"
kubectl apply -f "$rendered"
kubectl -n "$namespace" wait --for=condition=complete job/piraeus-storage-bench-forgejo --timeout=30m
client_pod=$(kubectl -n "$namespace" get pods \
  -l app.kubernetes.io/name=piraeus-storage-bench-forgejo -o json \
  | jq -er '[.items[] | select(.status.phase == "Succeeded")] | if length == 1 then .[0].metadata.name else error("expected one succeeded benchmark pod") end')
client_pod_json=$(kubectl -n "$namespace" get pod "$client_pod" -o json)
client_node=$(printf '%s' "$client_pod_json" | jq -r '.spec.nodeName')
if [ "$client_node" != ovh-ns102453 ]; then
  echo "benchmark client landed on $client_node; expected ovh-ns102453" >&2
  exit 1
fi
printf '%s' "$client_pod_json" | jq -r --arg target "$target" '
    . as $pod | .status.containerStatuses[] | select(.name == "benchmark") |
    [$target, "benchmark-client", $pod.spec.nodeName, .image, .imageID, "-", "-"] | @tsv
  ' >>"$runtime_file"
kubectl -n "$namespace" logs job/piraeus-storage-bench-forgejo >"$(dirname "$0")/results/forgejo-$target.csv"
kubectl -n "$namespace" delete job piraeus-storage-bench-forgejo --wait=true
