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
retry_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$retry_dir/.." && pwd)
results="$root/results/seaweedfs-hdd-retry-$attempt"
private_dir=${PIRAEUS_BENCH_PRIVATE_DIR:-/tmp/piraeus-storage-seaweed-retry$attempt-private}

ns_json=$(kubectl get namespace "$namespace" --ignore-not-found -o json)
if [ -z "$ns_json" ]; then
  remaining=$(kubectl get pv -o json | jq -r --arg ns "$namespace" '[.items[] | select(.spec.claimRef.namespace == $ns)] | length')
  if [ "$remaining" -ne 0 ]; then
    kubectl get pv -o json | jq -r --arg ns "$namespace" '.items[] | select(.spec.claimRef.namespace == $ns) | [.metadata.name,.metadata.uid,.spec.claimRef.name,.status.phase] | @tsv' >"$results/pv-remnants.txt"
    echo "namespace is absent but benchmark PVs remain; inspect normal CSI cleanup" >&2
    exit 2
  fi
  printf 'cleanup_at_utc=%s\nnamespace_absent=true\nbenchmark_pvs_remaining=0\n' "$(date -u +%FT%TZ)" >"$results/cleanup-proof.txt"
  exit 0
fi
owner_label=$(printf '%s' "$ns_json" | jq -r '.metadata.labels["benchmark.ducktape.io/owner"] // ""')
if [ "$owner_label" != "$owner" ]; then
  echo "refusing cleanup: namespace has an unexpected owner label" >&2
  exit 1
fi

if kubectl -n "$namespace" get job piraeus-storage-bench-forgejo >/dev/null 2>&1; then
  kubectl -n "$namespace" delete job piraeus-storage-bench-forgejo --wait=true
fi
if kubectl -n "$namespace" get deployment forgejo-bench >/dev/null 2>&1; then
  kubectl -n "$namespace" scale deployment/forgejo-bench --replicas=0
fi
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  active=$(kubectl -n "$namespace" get pods -o json | jq -r '[.items[] | select(.metadata.labels["app.kubernetes.io/instance"] == "forgejo-bench" or .metadata.labels["app.kubernetes.io/name"] == "piraeus-storage-bench-forgejo")] | length')
  [ "$active" -eq 0 ] && break
  sleep 5
done
active=$(kubectl -n "$namespace" get pods -o json | jq -r '[.items[] | select(.metadata.labels["app.kubernetes.io/instance"] == "forgejo-bench" or .metadata.labels["app.kubernetes.io/name"] == "piraeus-storage-bench-forgejo")] | length')
if [ "$active" -ne 0 ]; then
  echo "Forgejo or benchmark client Pods remain; refusing storage cleanup" >&2
  exit 3
fi

printf 'claim\tpvc_uid\tpv_name\tphase\tpv_uid\tvolume_handle\tcollection\n' >"$results/pvcs-before-cleanup.tsv"
kubectl -n "$namespace" get pvc -o json | jq -r '.items[] | [.metadata.name,.metadata.uid,.spec.volumeName,.status.phase] | @tsv' | while IFS="$(printf '\t')" read -r claim claim_uid pv phase; do
  [ -n "$pv" ] || continue
  pv_json=$(kubectl get pv "$pv" -o json)
  pv_uid=$(printf '%s' "$pv_json" | jq -r '.metadata.uid')
  volume_handle=$(printf '%s' "$pv_json" | jq -r '.spec.csi.volumeHandle // ""')
  collection=${volume_handle##*/}
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$claim" "$claim_uid" "$pv" "$phase" "$pv_uid" "$volume_handle" "$collection" >>"$results/pvcs-before-cleanup.tsv"
done
unexpected_claim=$(awk -F '\t' 'NR > 1 && $1 != "repo-seaweedfs" && $1 != "piraeus-storage-bench-db-1" {print $1}' "$results/pvcs-before-cleanup.tsv")
if [ -n "$unexpected_claim" ]; then
  echo "unexpected namespace PVCs present; refusing cleanup: $unexpected_claim" >&2
  exit 4
fi
printf 'claim\tpvc_uid\tpv_name\tpv_uid\tcollection\n' >"$results/owned-pvs-before-cleanup.tsv"
awk -F '\t' 'NR > 1 && NF >= 3 {print $1 "\t" $2 "\t" $3 "\t" $5 "\t" $7}' "$results/pvcs-before-cleanup.tsv" >>"$results/owned-pvs-before-cleanup.tsv"
printf 'claim\tpvc_uid\tpv_name\tpv_uid\treclaim_policy\n' >"$results/pv-cleanup.tsv"
printf 'pv_name\tvolume_attachment\tnode\n' >"$results/volume-attachments-before-cleanup.tsv"
while IFS="$(printf '\t')" read -r claim claim_uid pv pv_uid collection; do
  [ "$claim" = claim ] && continue
  [ -n "$pv" ] || continue
  attachment_rows=$(kubectl get volumeattachments -o json | jq -r --arg pv "$pv" '.items[] | select(.spec.source.persistentVolumeName == $pv) | [.metadata.name,.spec.nodeName] | @tsv')
  if [ -n "$attachment_rows" ]; then
    printf '%s\n' "$attachment_rows" | awk -F '\t' -v pv="$pv" '{print pv "\t" $1 "\t" $2}' >>"$results/volume-attachments-before-cleanup.tsv"
  fi
done <"$results/pvcs-before-cleanup.tsv"

if kubectl -n "$namespace" get database piraeus-bench-seaweedfs >/dev/null 2>&1; then
  kubectl -n "$namespace" delete database piraeus-bench-seaweedfs --wait=true --timeout=180s
fi
if kubectl -n "$namespace" get deployment piraeus-storage-bench-valkey >/dev/null 2>&1; then
  kubectl -n "$namespace" scale deployment/piraeus-storage-bench-valkey --replicas=0
  kubectl -n "$namespace" delete deployment piraeus-storage-bench-valkey --wait=true --timeout=180s
fi
if kubectl -n "$namespace" get cluster piraeus-storage-bench-db >/dev/null 2>&1; then
  kubectl -n "$namespace" delete cluster piraeus-storage-bench-db --wait=true --timeout=300s
fi
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  app_resources=$(kubectl -n "$namespace" get pods -o json | jq -r '[.items[] | select((.metadata.name|startswith("piraeus-storage-bench-db-")) or (.metadata.name|startswith("piraeus-storage-bench-valkey-")))] | length')
  [ "$app_resources" -eq 0 ] && break
  sleep 5
done
if [ "$app_resources" -ne 0 ]; then
  echo "PostgreSQL or Valkey Pods remain after their controllers were deleted" >&2
  exit 5
fi

while IFS="$(printf '\t')" read -r claim claim_uid pv pv_uid collection; do
  [ "$claim" = claim ] && continue
  [ -n "$pv" ] || continue
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    attachments=$(kubectl get volumeattachments -o json | jq -r --arg pv "$pv" '[.items[] | select(.spec.source.persistentVolumeName == $pv)] | length')
    [ "$attachments" -eq 0 ] && break
    sleep 5
  done
  attachments=$(kubectl get volumeattachments -o json | jq -r --arg pv "$pv" '[.items[] | select(.spec.source.persistentVolumeName == $pv)] | length')
  if [ "$attachments" -ne 0 ]; then
    echo "VolumeAttachment remains for stopped benchmark PV $pv; refusing PVC deletion" >&2
    exit 5
  fi
done <"$results/owned-pvs-before-cleanup.tsv"

for claim in repo-seaweedfs piraeus-storage-bench-db-1; do
  if ! kubectl -n "$namespace" get pvc "$claim" >/dev/null 2>&1; then
    continue
  fi
  pv=$(kubectl -n "$namespace" get pvc "$claim" -o jsonpath='{.spec.volumeName}')
  claim_uid=$(kubectl -n "$namespace" get pvc "$claim" -o jsonpath='{.metadata.uid}')
  pv_json=$(kubectl get pv "$pv" -o json)
  pv_uid=$(printf '%s' "$pv_json" | jq -r '.metadata.uid')
  pv_claim_ns=$(printf '%s' "$pv_json" | jq -r '.spec.claimRef.namespace')
  pv_claim_name=$(printf '%s' "$pv_json" | jq -r '.spec.claimRef.name')
  if [ "$pv_claim_ns" != "$namespace" ] || [ "$pv_claim_name" != "$claim" ]; then
    echo "PV $pv does not reference the exact benchmark PVC; refusing deletion" >&2
    exit 5
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$claim" "$claim_uid" "$pv" "$pv_uid" "$(printf '%s' "$pv_json" | jq -r '.spec.persistentVolumeReclaimPolicy')" >>"$results/pv-cleanup.tsv"
  kubectl -n "$namespace" delete pvc "$claim" --wait=true --timeout=180s
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
    pv_state=$(kubectl get pv "$pv" --ignore-not-found -o json)
    if [ -z "$pv_state" ] || [ "$pv_state" = null ]; then remaining=0; else remaining=1; fi
    [ "$remaining" -eq 0 ] && break
    sleep 10
  done
  pv_state=$(kubectl get pv "$pv" --ignore-not-found -o json)
  if [ -z "$pv_state" ] || [ "$pv_state" = null ]; then remaining=0; else remaining=1; fi
  if [ "$remaining" -ne 0 ]; then
    echo "benchmark PV $pv remains after normal PVC deletion; refusing to remove backend resources" >&2
    exit 6
  fi
done

while IFS="$(printf '\t')" read -r claim claim_uid pv pv_uid collection; do
  [ "$claim" = claim ] && continue
  [ -n "$pv" ] || continue
  pv_state=$(kubectl get pv "$pv" --ignore-not-found -o json)
  if [ -z "$pv_state" ] || [ "$pv_state" = null ]; then remaining=0; else remaining=1; fi
  if [ "$remaining" -ne 0 ]; then
    echo "recorded benchmark PV $pv remains after normal cleanup" >&2
    exit 6
  fi
  attachments=$(kubectl get volumeattachments -o json | jq -r --arg pv "$pv" '[.items[] | select(.spec.source.persistentVolumeName == $pv)] | length')
  if [ "$attachments" -ne 0 ]; then
    echo "VolumeAttachment still references cleaned benchmark PV $pv" >&2
    exit 6
  fi
done <"$results/owned-pvs-before-cleanup.tsv"

kubectl -n "$namespace" get pods -o json | jq -e '.items | length == 0' >/dev/null
kubectl -n "$namespace" get pvc -o json | jq -e '.items | length == 0' >/dev/null
kubectl delete namespace "$namespace" --wait=true --timeout=5m
remaining=$(kubectl get pv -o json | jq -r --arg ns "$namespace" '[.items[] | select(.spec.claimRef.namespace == $ns)] | length')
if [ "$remaining" -ne 0 ]; then
  echo "benchmark PVs remain after namespace deletion" >&2
  exit 7
fi

kubectl -n seaweedfs get pods -o json | jq -e '[.items[] | select(.metadata.name | test("^(seaweedfs-(filer-[01]|master-[012]|volume-hdd-[012])|seaweedfs-s3-.*)$"))] as $p | ($p | length) >= 10 and all($p[]; .status.phase == "Running" and all(.status.containerStatuses[]; .ready))' >/dev/null
kubectl -n seaweedfs-csi-system get pods -o json | jq -e '[.items[] | select(.spec.nodeName == "ovh-ns102453" and (.metadata.name | test("^seaweedfs-csi-driver-(mount|node)-")))] as $p | ($p | length) == 2 and all($p[]; .status.phase == "Running" and all(.status.containerStatuses[]; .ready))' >/dev/null
kubectl get sc seaweedfs-ovh -o json | jq '{metadata:{name:.metadata.name,uid:.metadata.uid},provisioner,volumeBindingMode,reclaimPolicy,parameters:(.parameters//{}),allowedTopologies}' >"$results/storageclass-after.json"
before_sc_uid=$(jq -r '.metadata.uid' "$results/storageclass-before.json")
after_sc_uid=$(jq -r '.metadata.uid' "$results/storageclass-after.json")
if [ "$before_sc_uid" != "$after_sc_uid" ]; then
  echo "shared SeaweedFS StorageClass identity changed during retry" >&2
  exit 8
fi

collection=$(awk -F '\t' '$1 == "repo-seaweedfs" {print $5}' "$results/owned-pvs-before-cleanup.tsv" 2>/dev/null | tail -n 1)
if [ -n "$collection" ]; then
  printf 'volume.list\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell >"$private_dir/seaweed-volume-list-after-cleanup.txt"
  awk -v collection="$collection" '/DataNode / {node=$0} /Disk / {disk=$0} index($0, "Collection:" collection ",") {print node "\n" disk "\n" $0}' "$private_dir/seaweed-volume-list-after-cleanup.txt" >"$results/seaweed-collection-after-cleanup.txt"
  count=$(rg -c "Collection:${collection}," "$results/seaweed-collection-after-cleanup.txt" || true)
  printf 'collection=%s\nremaining_volume_rows=%s\n' "$collection" "${count:-0}" >>"$results/seaweed-collection-after-cleanup.txt"
fi

printf 'cleanup_at_utc=%s\nnamespace_absent=true\nbenchmark_pvs_remaining=%s\nseaweed_provider=healthy\nseaweed_storageclass_uid=%s\n' "$(date -u +%FT%TZ)" "$remaining" "$after_sc_uid" >"$results/cleanup-proof.txt"
kubectl -n seaweedfs get pods -o json | jq -r '[.items[] | select(.metadata.name | test("^(seaweedfs-(filer-[01]|master-[012]|volume-hdd-[012])|seaweedfs-s3-.*)$")) | [.metadata.name,.status.phase,([.status.containerStatuses[]|select(.ready)]|length)] | @tsv] | .[]' >>"$results/cleanup-proof.txt"
