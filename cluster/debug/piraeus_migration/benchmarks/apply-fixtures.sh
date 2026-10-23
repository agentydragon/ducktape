#!/bin/sh
set -eu

namespace=piraeus-storage-bench-20261019
one_group=piraeus-bench-file-thin-one-1mib-20261019
two_group=piraeus-bench-file-thin-two-1mib-20261019
private_dir=${PIRAEUS_BENCH_PRIVATE_DIR:-/tmp/piraeus-storage-bench-private}

if kubectl get namespace "$namespace" >/dev/null 2>&1; then
  echo "namespace $namespace already exists; inspect ownership before continuing" >&2
  exit 1
fi
for storage_class in piraeus-bench-file-thin-one-1mib-20261019 piraeus-bench-file-thin-two-1mib-20261019; do
  if kubectl get storageclass "$storage_class" >/dev/null 2>&1; then
    echo "storage class $storage_class already exists; inspect ownership before continuing" >&2
    exit 1
  fi
done
if kubectl -n piraeus-datastore exec deploy/linstor-controller -- \
  linstor resource-group list | rg -q "$one_group|$two_group"; then
  echo "benchmark resource group already exists; inspect ownership before continuing" >&2
  exit 1
fi
kubectl create namespace "$namespace"
kubectl label namespace "$namespace" pod-security.kubernetes.io/enforce=baseline

mkdir -m 700 -p "$private_dir"
chmod 700 "$private_dir"
umask 077
printf 'bench' >"$private_dir/username"
od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$private_dir/password"
od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$private_dir/forgejo-secret-key"

kubectl create secret generic piraeus-storage-bench-db-app \
  --namespace "$namespace" \
  --from-file=username="$private_dir/username" \
  --from-file=password="$private_dir/password" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic piraeus-storage-bench-forgejo-key \
  --namespace "$namespace" \
  --from-file=secretKey="$private_dir/forgejo-secret-key" \
  --dry-run=client -o yaml | kubectl apply -f -

# The LINSTOR CSI provisioner creates each uniquely named resource group from the
# StorageClass parameters. It is checked again after provisioning below.
kubectl apply -f "$(dirname "$0")/fixtures.yaml"
kubectl create configmap piraeus-storage-bench-scripts \
  --namespace "$namespace" \
  --from-file=storage-bench.sh="$(dirname "$0")/storage-bench.sh" \
  --from-file=forgejo-bench.sh="$(dirname "$0")/forgejo-bench.sh" \
  --dry-run=client -o yaml | kubectl apply -f -
