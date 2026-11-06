#!/usr/bin/env bash
# Disposable single-guest QEMU TCG diagnostic. See ../tcg_reproduction.md.
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "Usage: $0 NEW_NAMESPACE INPUT_DIR OUTPUT_DIR [COUNT]" >&2
  exit 2
fi
namespace=$1
count=${4:-1}
if [[ ! "$count" =~ ^[1-9]$|^10$ ]]; then
  echo "COUNT must be between 1 and 10" >&2
  exit 2
fi
input_dir=$(realpath "$2")
mkdir -p "$3"
output_dir=$(realpath "$3")
script_dir=$(cd "$(dirname "$0")" && pwd)
for file in vmlinuz initramfs.cpio root.img test-runner.img; do
  [[ -f "$input_dir/$file" ]] || {
    echo "Missing $input_dir/$file" >&2
    exit 2
  }
done
if kubectl get namespace "$namespace" >/dev/null 2>&1; then
  echo "Refusing to touch existing namespace $namespace" >&2
  exit 2
fi
created_uid=''
cleanup() {
  local result=$? current_uid
  trap - EXIT INT TERM HUP
  if [[ -n "$created_uid" ]]; then
    current_uid=$(kubectl get namespace "$namespace" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)
    if [[ -n "$current_uid" && "$current_uid" != "$created_uid" ]]; then
      echo "Refusing cleanup: namespace UID changed" >&2
      exit 1
    fi
    if [[ -n "$current_uid" ]]; then
      if ! kubectl delete namespace "$namespace" --wait --timeout=180s; then
        echo "Failed to delete owned namespace $namespace (UID $created_uid)" >&2
        exit 1
      fi
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
created_uid=$(kubectl create namespace "$namespace" -o jsonpath='{.metadata.uid}')
[[ -n "$created_uid" ]] || {
  echo "Could not get namespace UID" >&2
  exit 1
}
kubectl label namespace "$namespace" pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/audit=restricted pod-security.kubernetes.io/warn=restricted
kubectl -n "$namespace" apply -f "$script_dir/pod.yaml"
kubectl -n "$namespace" wait --for=condition=Ready pod/qemu-smoke --timeout=300s
kubectl -n "$namespace" get pod qemu-smoke -o wide >"$output_dir/pod-placement.txt"
kubectl -n "$namespace" logs qemu-smoke >"$output_dir/pod-preflight.log"
# The launcher image's QEMU has a file capability; copying removes it so it can
# execute with no_new_privs in a restricted pod. The binary itself is unchanged.
kubectl -n "$namespace" exec qemu-smoke -- /bin/sh -ceu '
  cp /usr/libexec/qemu-kvm /scratch/qemu-kvm
  mkdir -p /scratch/empty-iso
  echo smoke > /scratch/empty-iso/placeholder
  xorriso -as mkisofs -quiet -o /scratch/empty.iso /scratch/empty-iso
'
stage() {
  kubectl -n "$namespace" exec -i qemu-smoke -- /bin/sh -c "cat > /scratch/$2" <"$1"
}
for file in vmlinuz initramfs.cpio root.img test-runner.img; do
  stage "$input_dir/$file" "$file"
done
stage "$script_dir/input.sha256" input.sha256
stage "$script_dir/guest.sh" guest.sh
failed=0
for ((iteration = 1; iteration <= count; iteration++)); do
  run_dir="$output_dir/run-$iteration"
  mkdir -p "$run_dir"
  # Reset only scratch data belonging to this pod; each guest gets a fresh disk.
  kubectl -n "$namespace" exec qemu-smoke -- /bin/sh -ceu '
    rm -rf /scratch/state2 /scratch/workspace.img
    mkdir /scratch/state2
    truncate -s 1G /scratch/workspace.img
  '
  result=0
  kubectl -n "$namespace" exec qemu-smoke -- /bin/sh /scratch/guest.sh >"$run_dir/guest-exec.log" 2>&1 || result=$?
  for log in guest-serial qemu-monitor virtiofsd; do
    kubectl -n "$namespace" exec qemu-smoke -- cat "/scratch/$log.log" >"$run_dir/$log.log" || true
  done
  if [[ "$result" != 0 ]] \
    || ! grep -Fq 'AGENTPLANE_GUEST_SHUTDOWN_REQUESTED' "$run_dir/guest-serial.log" \
    || ! grep -Fq 'AGENTPLANE_GUEST_SHUTDOWN_COMPLETE' "$run_dir/guest-serial.log" \
    || ! grep -Fq 'reboot: Power down' "$run_dir/guest-serial.log"; then
    echo "Run $iteration incomplete (QEMU exec exit $result); see $run_dir" >&2
    failed=1
  else
    echo "Run $iteration recorded request, shutdown receipt and powerdown"
  fi
done
kubectl -n "$namespace" get events --sort-by=.lastTimestamp >"$output_dir/events.txt" || true
exit "$failed"
