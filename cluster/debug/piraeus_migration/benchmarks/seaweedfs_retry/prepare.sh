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

if kubectl get namespace "$namespace" >/dev/null 2>&1; then
  echo "namespace $namespace already exists; inspect ownership before continuing" >&2
  exit 1
fi
if kubectl get pv -o json | jq -e --arg ns "$namespace" '.items[] | select(.spec.claimRef.namespace == $ns)' >/dev/null; then
  echo "a PV already references $namespace; inspect ownership before continuing" >&2
  exit 1
fi

for node in ovh-ns102453 ovh-ns104952; do
  kubectl get node "$node" -o json | jq -e 'any(.status.conditions[]; .type == "Ready" and .status == "True")' >/dev/null
done
kubectl get sc seaweedfs-ovh -o json | jq -e '.provisioner == "seaweedfs-csi-driver" and .volumeBindingMode == "WaitForFirstConsumer" and .reclaimPolicy == "Delete" and ([.allowedTopologies[]?.matchLabelExpressions[]? | select(.key == "topology.kubernetes.io/zone" and (.values | index("hil-ovh")))] | length > 0)' >/dev/null
kubectl -n seaweedfs get pods -o json | jq -e '[.items[] | select(.metadata.name | test("^(seaweedfs-(filer-[01]|master-[012]|volume-hdd-[012])|seaweedfs-s3-.*)$"))] as $p | ($p | length) >= 10 and all($p[]; .status.phase == "Running" and all(.status.containerStatuses[]; .ready))' >/dev/null
kubectl -n seaweedfs-csi-system get pods -o json | jq -e '[.items[] | select(.spec.nodeName == "ovh-ns102453" and (.metadata.name | test("^seaweedfs-csi-driver-(mount|node)-")))] as $p | ($p | length) == 2 and all($p[]; .status.phase == "Running" and all(.status.containerStatuses[]; .ready))' >/dev/null

mkdir -m 700 -p "$private_dir" "$results"
chmod 700 "$private_dir"
umask 077
printf 'volume.list\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell >"$private_dir/seaweed-volume-list-before.txt"
awk '/^Topology volumeSizeLimit:/{print; exit}' "$private_dir/seaweed-volume-list-before.txt" >"$results/provider-capacity-before.txt"
kubectl get sc seaweedfs-ovh -o json | jq '{metadata:{name:.metadata.name,uid:.metadata.uid},provisioner,volumeBindingMode,reclaimPolicy,parameters:(.parameters//{}),allowedTopologies}' >"$results/storageclass-before.json"
kubectl -n seaweedfs get pods -o json | jq -r '[.items[] | select(.metadata.name | test("^(seaweedfs-(filer-[01]|master-[012]|volume-hdd-[012])|seaweedfs-s3-.*)$")) | [.metadata.name,.status.phase,([.status.containerStatuses[]|select(.ready)]|length)] | @tsv] | .[]' >"$results/provider-pods-before.tsv"
kubectl -n seaweedfs-csi-system get pods -o json | jq -r '[.items[] | select(.spec.nodeName == "ovh-ns102453" and (.metadata.name | test("^seaweedfs-csi-driver-(mount|node)-"))) | [.metadata.name,.spec.nodeName,.status.phase,([.status.containerStatuses[]|select(.ready)]|length)] | @tsv] | .[]' >"$results/csi-pods-before.tsv"

printf 'attempt=%s\npreflight_at_utc=%s\nnamespace_absent=true\n' "$attempt" "$(date -u +%FT%TZ)" >"$results/preflight.txt"
printf 'target_node=ovh-ns102453 Ready\ndb_node=ovh-ns104952 Ready\n' >>"$results/preflight.txt"
printf 'storage_class=seaweedfs-ovh\nmedia_selection=media-blind\nrequested_repo_volume=1Gi\n' >>"$results/preflight.txt"
printf 'The original failed Forgejo cell reused its direct-filesystem test PVC before the first app mount. This retry uses a new PVC and first app mount; success would not establish why the old attempt failed or that remount behavior changed.\n' >>"$results/preflight.txt"

printf 'bench' >"$private_dir/username"
od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$private_dir/password"
od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$private_dir/forgejo-secret-key"

kubectl create namespace "$namespace"
kubectl label namespace "$namespace" "benchmark.ducktape.io/owner=$owner" pod-security.kubernetes.io/enforce=baseline --overwrite
kubectl create secret generic piraeus-storage-bench-db-app --namespace "$namespace" --from-file=username="$private_dir/username" --from-file=password="$private_dir/password" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic piraeus-storage-bench-forgejo-key --namespace "$namespace" --from-file=secretKey="$private_dir/forgejo-secret-key" --dry-run=client -o yaml | kubectl apply -f -
sed -e "s/piraeus-storage-bench-20261019-seaweed-retry1/$namespace/g" -e "s/seaweedfs-hdd-retry-1/$owner/g" "$retry_dir/fixtures.yaml" >"$private_dir/fixtures.yaml"
kubectl apply -f "$private_dir/fixtures.yaml"
kubectl create configmap piraeus-storage-bench-scripts --namespace "$namespace" --from-file=forgejo-bench.sh="$root/forgejo-bench.sh" --dry-run=client -o yaml | kubectl apply -f -

printf 'fixtures_applied_at_utc=%s\nnamespace=%s\nowner_label=%s\nrepo_claim=repo-seaweedfs\nrepo_size=1Gi\n' "$(date -u +%FT%TZ)" "$namespace" "$owner" >>"$results/preflight.txt"
