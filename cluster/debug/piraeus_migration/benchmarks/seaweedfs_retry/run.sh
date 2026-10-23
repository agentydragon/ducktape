#!/bin/sh
set -eu

attempt=${PIRAEUS_BENCH_ATTEMPT:-1}
case "$attempt" in
  1 | 2) ;;
  *)
    echo "PIRAEUS_BENCH_ATTEMPT must be 1 or 2" >&2
    exit 1
    ;;
esac
namespace="piraeus-storage-bench-20261019-seaweed-retry$attempt"
owner="seaweedfs-hdd-retry-$attempt"
private_dir=${PIRAEUS_BENCH_PRIVATE_DIR:-/tmp/piraeus-storage-seaweed-retry$attempt-private}
retry_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$retry_dir/.." && pwd)
results="$root/results/seaweedfs-hdd-retry-$attempt"
csv="$root/results/forgejo-seaweedfs-ovh.csv"
chart="$private_dir/helm/verify/forgejo-17.1.1.tgz"
chart_digest=sha256:a99352bc5167aba8b58f0de386e777e7fe742bdd573daf82f38986239ff52128
chart_archive_sha=26aa3120e020b67593ce6e2266c457ee925ff514f606563e184b09ed6581e18f
app_digest=sha256:8f97b55ca162ef3b538c6c78a2a077df9f2143c41d80b2bc6b6920f8d430df34
db_digest=sha256:51bc94428be3df3e234447278e33803544a93d27a57a4b6af737f218d6472d12
valkey_digest=sha256:a0dbf4c1d5708782907c10e2c72deff317518518b5288a58416981d9db95d30b

if [ -e "$csv" ]; then
  if [ "$(head -n 1 "$csv")" != target,operation,http_status,seconds,attempt ]; then
    echo "unexpected pooled CSV header: $csv" >&2
    exit 1
  fi
  if awk -F, -v attempt="$attempt" 'NR > 1 && $5 == attempt {found=1} END {exit !found}' "$csv"; then
    echo "refusing to overwrite recorded attempt $attempt in $csv" >&2
    exit 1
  fi
fi
mkdir -m 700 -p "$private_dir" "$results" "$private_dir/helm/cache" "$private_dir/helm/config" "$private_dir/helm/data" "$private_dir/helm/verify"
chmod 700 "$private_dir"
owner_label=$(kubectl get namespace "$namespace" -o jsonpath='{.metadata.labels.benchmark\.ducktape\.io/owner}')
if [ "$owner_label" != "$owner" ]; then
  echo "refusing to operate without the expected namespace ownership label" >&2
  exit 1
fi

capture_scoped_csi_logs() {
  failed_pod=$1
  failed_pod_uid=$(kubectl -n "$namespace" get pod "$failed_pod" -o jsonpath='{.metadata.uid}')
  pvc_uid=$(kubectl -n "$namespace" get pvc repo-seaweedfs -o jsonpath='{.metadata.uid}' 2>/dev/null || true)
  collection=
  if kubectl -n "$namespace" get pvc repo-seaweedfs -o jsonpath='{.spec.volumeName}' >/dev/null 2>&1; then
    failed_pv=$(kubectl -n "$namespace" get pvc repo-seaweedfs -o jsonpath='{.spec.volumeName}')
    collection=$(kubectl get pv "$failed_pv" -o jsonpath='{.spec.csi.volumeHandle}' | awk -F/ '{print $NF}')
  fi
  printf 'captured_at_utc=%s\nnamespace=%s\napp_pod_uid=%s\npvc_uid=%s\ncollection=%s\n' "$(date -u +%FT%TZ)" "$namespace" "$failed_pod_uid" "$pvc_uid" "$collection" >"$results/seaweed-csi-failure.txt"
  csi_pods=$(kubectl -n seaweedfs-csi-system get pods -o json | jq -r '.items[] | select(.spec.nodeName == "ovh-ns102453" and (.metadata.name | test("^seaweedfs-csi-driver-(mount|node)-"))) | .metadata.name')
  for csi_pod in $csi_pods; do
    containers=$(kubectl -n seaweedfs-csi-system get pod "$csi_pod" -o json | jq -r '.spec.containers[].name | select(. == "seaweedfs-mount" or . == "csi-seaweedfs-plugin")')
    for container in $containers; do
      printf '\nsource=%s container=%s\n' "$csi_pod" "$container" >>"$results/seaweed-csi-failure.txt"
      kubectl -n seaweedfs-csi-system logs "$csi_pod" -c "$container" --since=20m 2>/dev/null | awk -v pvc="$pvc_uid" -v collection="$collection" -v pod="$failed_pod_uid" '((pvc != "") && index($0,pvc)) || ((collection != "") && index($0,collection)) || ((pod != "") && index($0,pod))' >>"$results/seaweed-csi-failure.txt" || true
    done
  done
}

collect_placement() {
  phase=$1
  printf 'volume.list\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell >"$private_dir/seaweed-volume-list-$phase.txt"
  awk -v collection="$collection" '/DataNode / {node=$0} /Disk / {disk=$0} index($0, "Collection:" collection ",") {print node "\n" disk "\n" $0}' "$private_dir/seaweed-volume-list-$phase.txt" >"$results/seaweed-collection-placement-$phase.txt"
  count=$(rg -c "Collection:${collection}," "$results/seaweed-collection-placement-$phase.txt" || true)
  printf 'phase=%s\ncollection=%s\nvolume_rows=%s\nclass=seaweedfs-ovh (media-blind)\n' "$phase" "$collection" "${count:-0}" >"$results/media-check-$phase.txt"
  if [ "${count:-0}" -gt 0 ]; then
    disks=$(awk '$1 == "Disk" {name=$2; sub(/\(.*/, "", name); print name}' "$results/seaweed-collection-placement-$phase.txt" | sort -u | paste -sd, -)
    printf 'observed_disks=%s\n' "$disks" >>"$results/media-check-$phase.txt"
    if [ "$disks" = hdd ]; then
      printf 'collection_media=HDD only\n' >>"$results/media-check-$phase.txt"
    else
      printf 'collection_media=not HDD-only or mixed; see scoped placement rows\n' >>"$results/media-check-$phase.txt"
    fi
  else
    printf 'collection_media=not observable at this phase\n' >>"$results/media-check-$phase.txt"
  fi
}

kubectl -n "$namespace" wait --for=condition=Ready cluster/piraeus-storage-bench-db --timeout=15m
kubectl -n "$namespace" rollout status deployment/piraeus-storage-bench-valkey --timeout=10m
kubectl -n "$namespace" wait --for=jsonpath='{.status.applied}'=true database/piraeus-bench-seaweedfs --timeout=15m

if [ ! -f "$chart" ]; then
  direnv exec . env HELM_CACHE_HOME="$private_dir/helm/cache" HELM_CONFIG_HOME="$private_dir/helm/config" HELM_DATA_HOME="$private_dir/helm/data" helm pull oci://code.forgejo.org/forgejo-helm/forgejo --version 17.1.1 --destination "$private_dir/helm/verify" >"$private_dir/chart-pull.txt" 2>&1
fi
actual_chart_digest=$(sha256sum "$chart" | awk '{print $1}')
if [ "$actual_chart_digest" != "$chart_archive_sha" ]; then
  echo "unexpected Forgejo chart archive SHA-256 $actual_chart_digest" >&2
  exit 1
fi
observed_chart_digest=$(awk '/^Digest: / {print $2}' "$private_dir/chart-pull.txt")
if [ "$observed_chart_digest" != "$chart_digest" ]; then
  echo "unexpected Forgejo OCI chart digest $observed_chart_digest" >&2
  exit 1
fi
sed "s/piraeus-storage-bench-20261019-seaweed-retry1/$namespace/g" "$retry_dir/forgejo-values.yaml" >"$private_dir/forgejo-values.yaml"
sed "s/piraeus-storage-bench-20261019-seaweed-retry1/$namespace/g" "$retry_dir/forgejo-job.yaml" >"$private_dir/forgejo-job.yaml"
direnv exec . env HELM_CACHE_HOME="$private_dir/helm/cache" HELM_CONFIG_HOME="$private_dir/helm/config" HELM_DATA_HOME="$private_dir/helm/data" helm template forgejo-bench "$chart" --namespace "$namespace" --values "$private_dir/forgejo-values.yaml" --set-string image.digest="$app_digest" --set-string gitea.config.database.NAME=bench_seaweedfs --set-string persistence.claimName=repo-seaweedfs >"$private_dir/forgejo-rendered.yaml"
app_ref="code.forgejo.org/forgejo/forgejo:15.0.8-rootless@$app_digest"
if ! rg -F "$app_ref" "$private_dir/forgejo-rendered.yaml" >/dev/null; then
  rg -n 'image:' "$private_dir/forgejo-rendered.yaml" >&2
  echo "rendered Forgejo image did not match the pinned tag and digest" >&2
  exit 1
fi
printf 'chart_oci_manifest_digest=%s\nchart_archive_sha256=%s\nforgejo_image=%s\npostgres_image=ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie@%s\nvalkey_image=docker.io/valkey/valkey@%s\n' "$chart_digest" "$chart_archive_sha" "$app_ref" "$db_digest" "$valkey_digest" >"$results/pinned-artifacts.txt"
printf 'helm_install_started_at_utc=%s\n' "$(date -u +%FT%TZ)" >>"$results/pinned-artifacts.txt"
direnv exec . env HELM_CACHE_HOME="$private_dir/helm/cache" HELM_CONFIG_HOME="$private_dir/helm/config" HELM_DATA_HOME="$private_dir/helm/data" helm upgrade --install forgejo-bench "$chart" --namespace "$namespace" --values "$private_dir/forgejo-values.yaml" --set-string image.digest="$app_digest" --set-string gitea.config.database.NAME=bench_seaweedfs --set-string persistence.claimName=repo-seaweedfs >"$results/helm-install.txt"

printf 'observed_at_utc\tpod\tphase\tnode\trestarts\tinit_status\tcontainer_status\twarning_events\n' >"$results/pod-startup.tsv"
app_pod=
deadline=$(($(date +%s) + 240))
stationary_since=0
last_signature=
while [ "$(date +%s)" -lt "$deadline" ]; do
  pods=$(kubectl -n "$namespace" get pods -l app.kubernetes.io/instance=forgejo-bench -o json)
  app_pod=$(printf '%s' "$pods" | jq -r '[.items[]] | if length == 1 then .[0].metadata.name else empty end')
  if [ -z "$app_pod" ]; then
    sleep 5
    continue
  fi
  pod_json=$(kubectl -n "$namespace" get pod "$app_pod" -o json)
  pod_uid=$(printf '%s' "$pod_json" | jq -r '.metadata.uid')
  phase=$(printf '%s' "$pod_json" | jq -r '.status.phase // "Unknown"')
  node=$(printf '%s' "$pod_json" | jq -r '.spec.nodeName // ""')
  restarts=$(printf '%s' "$pod_json" | jq '[.status.initContainerStatuses[]?.restartCount, .status.containerStatuses[]?.restartCount] | add // 0')
  init_status=$(printf '%s' "$pod_json" | jq -r '[.status.initContainerStatuses[]? | [.name,(.restartCount|tostring),(.state.waiting.reason//""),(.state.waiting.message//""),(.lastState.terminated.reason//""),(.lastState.terminated.exitCode//""|tostring)] | @tsv] | join("; ")')
  container_status=$(printf '%s' "$pod_json" | jq -r '[.status.containerStatuses[]? | [.name,(.restartCount|tostring),(.state.waiting.reason//""),(.state.waiting.message//""),(.state.running.startedAt//""),(.lastState.terminated.reason//""),(.lastState.terminated.exitCode//""|tostring)] | @tsv] | join("; ")')
  events=$(kubectl -n "$namespace" get events --field-selector "involvedObject.uid=$pod_uid" -o json | jq -r '[.items[] | select(.type == "Warning") | [.reason,.message] | @tsv] | join("; ")')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$app_pod" "$phase" "$node" "$restarts" "$init_status" "$container_status" "$events" >>"$results/pod-startup.tsv"
  if printf '%s' "$pod_json" | jq -e '.status.phase == "Running" and all(.status.containerStatuses[]?; .ready == true)' >/dev/null; then
    break
  fi
  active=$(printf '%s' "$pod_json" | jq '[.status.initContainerStatuses[]?.state.running, .status.containerStatuses[]?.state.running] | any(. != null)')
  signature="$phase|$node|$restarts|$init_status|$container_status|$events"
  if [ "$active" != true ]; then
    if [ "$signature" != "$last_signature" ]; then
      stationary_since=$(date +%s)
      last_signature=$signature
    fi
    if [ "$restarts" -ge 3 ] || [ "$(($(date +%s) - stationary_since))" -ge 120 ]; then
      printf 'setup_failure_detected_at_utc=%s\nrestart_count=%s\nstationary_seconds=%s\n' "$(date -u +%FT%TZ)" "$restarts" "$(($(date +%s) - stationary_since))" >"$results/setup-failure.txt"
      printf '%s\n' "$init_status" "$container_status" "$events" >>"$results/setup-failure.txt"
      printf '%s' "$pod_json" | jq '{metadata:{name:.metadata.name,uid:.metadata.uid,creationTimestamp:.metadata.creationTimestamp},spec:{nodeName:.spec.nodeName,volumes:[.spec.volumes[]|select(.persistentVolumeClaim)|.persistentVolumeClaim.claimName]},status:{phase:.status.phase,initContainerStatuses:(.status.initContainerStatuses//[]|map({name,restartCount,state,lastState})),containerStatuses:(.status.containerStatuses//[]|map({name,restartCount,state,lastState}))}}' >"$results/setup-failure-pod.json"
      kubectl -n "$namespace" get events --field-selector "involvedObject.uid=$pod_uid" --sort-by=.lastTimestamp >"$results/setup-failure-events.txt"
      capture_scoped_csi_logs "$app_pod" || true
      exit 20
    fi
  else
    stationary_since=$(date +%s)
    last_signature=$signature
  fi
  sleep 10
done

if [ -z "$app_pod" ] || ! kubectl -n "$namespace" get pod "$app_pod" -o json | jq -e '.status.phase == "Running" and all(.status.containerStatuses[]?; .ready == true)' >/dev/null; then
  printf 'setup_failure_detected_at_utc=%s\nreason=Forgejo did not become Ready within bounded 240 second startup window\n' "$(date -u +%FT%TZ)" >"$results/setup-failure.txt"
  if [ -n "$app_pod" ]; then capture_scoped_csi_logs "$app_pod" || true; fi
  exit 21
fi

pod_json=$(kubectl -n "$namespace" get pod "$app_pod" -o json)
app_node=$(printf '%s' "$pod_json" | jq -r '.spec.nodeName')
app_pvc=$(printf '%s' "$pod_json" | jq -er '[.spec.volumes[] | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName] | if length == 1 then .[0] else error("expected exactly one repository PVC") end')
app_image_id=$(printf '%s' "$pod_json" | jq -er '.status.containerStatuses[] | select(.name == "forgejo") | .imageID')
if [ "$app_node" != ovh-ns102453 ] || [ "$app_pvc" != repo-seaweedfs ] || ! printf '%s' "$app_image_id" | rg -F "$app_digest" >/dev/null; then
  echo "Forgejo runtime did not match pinned node/PVC/image expectations" >&2
  exit 22
fi

repo_pvc=$(kubectl -n "$namespace" get pvc repo-seaweedfs -o json)
pvc_uid=$(printf '%s' "$repo_pvc" | jq -r '.metadata.uid')
pv_name=$(printf '%s' "$repo_pvc" | jq -r '.spec.volumeName')
pv_json=$(kubectl get pv "$pv_name" -o json)
pv_uid=$(printf '%s' "$pv_json" | jq -r '.metadata.uid')
volume_handle=$(printf '%s' "$pv_json" | jq -r '.spec.csi.volumeHandle')
collection=${volume_handle##*/}
if [ -z "$collection" ] || [ "$collection" = "$volume_handle" ]; then
  echo "Seaweed PV has an unexpected CSI volume handle; cannot scope placement proof" >&2
  exit 23
fi
printf 'app_started_at_utc=%s\napp_pod=%s\napp_pod_uid=%s\napp_node=%s\napp_image_id=%s\napp_pvc=%s\npvc_uid=%s\npv_name=%s\npv_uid=%s\nvolume_handle=%s\ncollection=%s\n' "$(date -u +%FT%TZ)" "$app_pod" "$(printf '%s' "$pod_json" | jq -r '.metadata.uid')" "$app_node" "$app_image_id" "$app_pvc" "$pvc_uid" "$pv_name" "$pv_uid" "$volume_handle" "$collection" >"$results/runtime.txt"
printf 'target\tcomponent\tnode\timage\timage_id\tpvc_expected\tpvc_actual\n' >"$results/runtime.tsv"
printf 'seaweedfs-ovh\tforgejo\t%s\t%s\t%s\t%s\t%s\n' "$app_node" "$(printf '%s' "$pod_json" | jq -r '.status.containerStatuses[] | select(.name=="forgejo") | .image')" "$app_image_id" "$app_pvc" "$app_pvc" >>"$results/runtime.tsv"
kubectl -n "$namespace" get pods -o json | jq -r '.items[] as $p | $p.status.containerStatuses[]? as $c | select(($p.metadata.name|startswith("piraeus-storage-bench-db-")) or ($p.metadata.name|startswith("piraeus-storage-bench-valkey-"))) | [$p.metadata.name,$p.spec.nodeName,$c.name,$c.image,$c.imageID] | @tsv' >>"$results/runtime.txt"
db_pod=$(kubectl -n "$namespace" get pods -o json | jq -er '[.items[] | select(.metadata.name|startswith("piraeus-storage-bench-db-")) | select(.status.phase == "Running")] | if length == 1 then .[0].metadata.name else error("expected one running fixed PostgreSQL Pod") end')
db_json=$(kubectl -n "$namespace" get pod "$db_pod" -o json)
db_node=$(printf '%s' "$db_json" | jq -r '.spec.nodeName')
db_image_id=$(printf '%s' "$db_json" | jq -er '.status.containerStatuses[] | select(.name == "postgres") | .imageID')
valkey_pod=$(kubectl -n "$namespace" get pods -o json | jq -er '[.items[] | select(.metadata.name|startswith("piraeus-storage-bench-valkey-")) | select(.status.phase == "Running")] | if length == 1 then .[0].metadata.name else error("expected one running fixed Valkey Pod") end')
valkey_json=$(kubectl -n "$namespace" get pod "$valkey_pod" -o json)
valkey_node=$(printf '%s' "$valkey_json" | jq -r '.spec.nodeName')
valkey_image_id=$(printf '%s' "$valkey_json" | jq -er '.status.containerStatuses[] | select(.name == "valkey") | .imageID')
if [ "$db_node" != ovh-ns104952 ] || ! printf '%s' "$db_image_id" | rg -F "$db_digest" >/dev/null; then
  echo "PostgreSQL did not match the fixed SSD node and pinned image" >&2
  exit 27
fi
if [ "$valkey_node" != ovh-ns104952 ] || ! printf '%s' "$valkey_image_id" | rg -F "$valkey_digest" >/dev/null; then
  echo "Valkey did not match the fixed node and pinned image" >&2
  exit 28
fi
db_pvc=$(kubectl -n "$namespace" get pvc piraeus-storage-bench-db-1 -o json)
printf 'postgres_pod=%s\npostgres_node=%s\npostgres_image_id=%s\npostgres_pvc=%s\npostgres_pv=%s\npostgres_pv_uid=%s\nvalkey_pod=%s\nvalkey_node=%s\nvalkey_image_id=%s\n' "$db_pod" "$db_node" "$db_image_id" "$(printf '%s' "$db_pvc" | jq -r '.metadata.name')" "$(printf '%s' "$db_pvc" | jq -r '.spec.volumeName')" "$(kubectl get pv "$(printf '%s' "$db_pvc" | jq -r '.spec.volumeName')" -o jsonpath='{.metadata.uid}')" "$valkey_pod" "$valkey_node" "$valkey_image_id" >>"$results/runtime.txt"
kubectl -n "$namespace" exec "$app_pod" -c forgejo -- df -T /data | tail -n 1 >>"$results/runtime.txt"
kubectl -n "$namespace" exec "$app_pod" -c forgejo -- /usr/local/bin/forgejo --version >>"$results/runtime.txt" 2>&1 || true
kubectl -n "$namespace" exec deployment/piraeus-storage-bench-valkey -- valkey-cli INFO server | rg '^valkey_version:' >>"$results/runtime.txt"

collect_placement before
kubectl apply -f "$private_dir/forgejo-job.yaml"
job_pod=
deadline=$(($(date +%s) + 60))
while [ "$(date +%s)" -lt "$deadline" ]; do
  job_pod=$(kubectl -n "$namespace" get pods -l app.kubernetes.io/name=piraeus-storage-bench-forgejo -o json | jq -r '[.items[]] | if length == 1 then .[0].metadata.name else empty end')
  [ -n "$job_pod" ] && break
  sleep 2
done
if [ -z "$job_pod" ]; then
  echo "benchmark client Pod did not appear" >&2
  exit 24
fi
client_json=$(kubectl -n "$namespace" get pod "$job_pod" -o json)
client_node=$(printf '%s' "$client_json" | jq -r '.spec.nodeName')
client_image_id=$(printf '%s' "$client_json" | jq -r '.status.containerStatuses[]? | select(.name == "benchmark") | .imageID // empty')
client_deadline=$(($(date +%s) + 90))
while [ -z "$client_image_id" ] && [ "$(date +%s)" -lt "$client_deadline" ]; do
  sleep 2
  client_json=$(kubectl -n "$namespace" get pod "$job_pod" -o json)
  client_node=$(printf '%s' "$client_json" | jq -r '.spec.nodeName')
  client_image_id=$(printf '%s' "$client_json" | jq -r '.status.containerStatuses[]? | select(.name == "benchmark") | .imageID // empty')
done
if [ -z "$client_image_id" ]; then
  echo "benchmark client did not report a pinned image ID" >&2
  exit 29
fi
if [ "$client_node" != ovh-ns102453 ] || ! printf '%s' "$client_image_id" | rg -F "$app_digest" >/dev/null; then
  echo "benchmark client node/image did not match pinned expectations" >&2
  exit 25
fi
printf 'seaweedfs-ovh\tbenchmark-client\t%s\t%s\t%s\t-\t-\n' "$client_node" "$(printf '%s' "$client_json" | jq -r '.spec.containers[] | select(.name=="benchmark") | .image')" "$client_image_id" >>"$results/runtime.tsv"
if ! kubectl -n "$namespace" wait --for=condition=complete job/piraeus-storage-bench-forgejo --timeout=15m; then
  kubectl -n "$namespace" logs job/piraeus-storage-bench-forgejo >"$private_dir/forgejo-seaweedfs-failed.log" 2>&1 || true
  kubectl -n "$namespace" get job piraeus-storage-bench-forgejo -o json | jq '{metadata:{name:.metadata.name,uid:.metadata.uid},status}' >"$results/job-failure.json"
  echo "bounded Forgejo workload Job did not complete" >&2
  exit 30
fi
kubectl -n "$namespace" logs job/piraeus-storage-bench-forgejo >"$private_dir/forgejo-seaweedfs-raw.csv"
rows=$(awk 'END {print NR-1}' "$private_dir/forgejo-seaweedfs-raw.csv")
if [ "$rows" -ne 80 ] || [ "$(awk -F, 'NR>1 {if ($1!="seaweedfs-ovh" || $3+0<200 || $3+0>=300) bad=1; count[$2]++} END {if (count["version"]!=20 || count["contents_read"]!=20 || count["contents_write"]!=20 || count["git_push"]!=20) bad=1; print bad+0}' "$private_dir/forgejo-seaweedfs-raw.csv")" -ne 0 ]; then
  echo "expected 80 successful SeaweedFS samples (20 per operation); raw output remains private" >&2
  exit 26
fi
collect_placement after
{
  if [ -e "$csv" ]; then cat "$csv"; else printf 'target,operation,http_status,seconds,attempt\n'; fi
  awk -v attempt="$attempt" 'NR > 1 {print $0 "," attempt}' "$private_dir/forgejo-seaweedfs-raw.csv"
} >"$private_dir/forgejo-seaweedfs-pooled.csv"
cp "$private_dir/forgejo-seaweedfs-pooled.csv" "$csv"
awk -F, 'NR>1 {count[$2]++} END {for (op in count) printf "%s\t%d\n",op,count[op]}' "$private_dir/forgejo-seaweedfs-raw.csv" | sort >"$results/sample-counts.tsv"
printf 'completed_at_utc=%s\naccepted_rows=%s\n' "$(date -u +%FT%TZ)" "$rows" >>"$results/runtime.txt"
kubectl -n "$namespace" delete job piraeus-storage-bench-forgejo --wait=true
kubectl -n "$namespace" get pods -l app.kubernetes.io/name=piraeus-storage-bench-forgejo -o json | jq -e '.items | length == 0' >/dev/null
