#!/usr/bin/env bash
# Probes the action's kernel, then tries k3s server configurations in turn until one runs a Pod
# with a local-path PVC, a non-root securityContext and pod-to-pod traffic. Prints a summary line
# per attempt; full k3s logs go to the undeclared outputs.
set -uo pipefail

K3S_VERSION="v1.33.4+k3s1"
OUT="${TEST_UNDECLARED_OUTPUTS_DIR:-/tmp/k3s-spike-out}"
mkdir -p "$OUT"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo -E"
t0=$(date +%s)
ts() { echo "[+$(($(date +%s) - t0))s] $*"; }

ts "== environment"
uname -a
id
nproc
free -m | head -2
df -h / /tmp | tail -2
stat -fc 'cgroup fs: %T' /sys/fs/cgroup
cat /sys/fs/cgroup/cgroup.controllers 2>&1
iptables --version 2>&1
$SUDO iptables -t nat -L -n >/dev/null 2>&1 && echo "iptables nat: ok" || echo "iptables nat: FAIL"
for m in overlay br_netfilter vxlan ip_tables nf_conntrack xt_conntrack iptable_nat; do
  if [ -d "/sys/module/$m" ]; then echo "module $m: loaded"; else $SUDO modprobe "$m" 2>/dev/null && echo "module $m: modprobed" || echo "module $m: MISSING"; fi
done
grep -w overlay /proc/filesystems || echo "overlay fs: MISSING"
[ -e /proc/config.gz ] && zcat /proc/config.gz | grep -E 'CONFIG_(BRIDGE_NETFILTER|VXLAN|NETFILTER_XT_|IP_NF_|NF_TABLES|NFT_|NF_NAT|CGROUP_BPF|USER_NS|OVERLAY_FS|KEYS|IP_SET|BPF_SYSCALL)'
ls /dev/kvm 2>&1

ts "== download k3s $K3S_VERSION"
base="https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION//+/%2B}"
work=$(mktemp -d)
curl -fsSL -o "$work/k3s" "$base/k3s" || {
  echo "download k3s FAILED"
  exit 1
}
curl -fsSL -o "$work/airgap.tar.zst" "$base/k3s-airgap-images-amd64.tar.zst" || {
  echo "download airgap FAILED"
  exit 1
}
chmod +x "$work/k3s"
ts "downloaded: $(du -sh "$work" | cut -f1)"

# The Firecracker guest kernel has no CONFIG_KEYS, and kubelet refuses to start when it cannot read
# kernel/keys/root_max{keys,bytes}. Shadow /proc/sys/kernel with a copy that has them. Containers
# mount their own procfs, so only the host-side k3s processes see the copy.
fake_kernel_sysctls() {
  [ -e /proc/sys/kernel/keys/root_maxkeys ] && return 0
  local fake="$work/proc-sys-kernel"
  mkdir -p "$fake/keys"
  (cd /proc/sys/kernel && find . -type d) | while read -r d; do mkdir -p "$fake/$d"; done
  (cd /proc/sys/kernel && find . -type f -readable) | while read -r f; do cat "/proc/sys/kernel/$f" >"$fake/$f" 2>/dev/null || true; done
  echo 1000000 >"$fake/keys/root_maxkeys"
  echo 25000000 >"$fake/keys/root_maxbytes"
  $SUDO mount --bind "$fake" /proc/sys/kernel && echo "shadowed /proc/sys/kernel"
}

cleanup_k3s() {
  $SUDO pkill -TERM -f 'k3s server' 2>/dev/null
  sleep 3
  $SUDO pkill -KILL -f 'k3s server' 2>/dev/null
  $SUDO pkill -KILL -f containerd-shim 2>/dev/null
  mount | awk '/\/run\/k3s|\/var\/lib\/kubelet|k3s-spike/ {print $3}' | sort -r | xargs -r -n1 $SUDO umount -l 2>/dev/null
  $SUDO ip link delete cni0 2>/dev/null
  $SUDO ip link delete flannel.1 2>/dev/null
}
trap cleanup_k3s EXIT

POD_YAML=$(
  cat <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: state}
spec: {accessModes: [ReadWriteOnce], storageClassName: local-path, resources: {requests: {storage: 64Mi}}}
---
apiVersion: v1
kind: Pod
metadata: {name: server, labels: {app: server}}
spec:
  securityContext: {runAsUser: 107, runAsGroup: 107, fsGroup: 107}
  containers:
  - name: c
    image: BUSYBOX
    command: [sh, -c, 'echo hello-from-pvc > /state/f && mkdir -p /www && cp /state/f /www/index.html && httpd -f -p 8080 -h /www']
    volumeMounts: [{name: state, mountPath: /state}]
    readinessProbe: {exec: {command: [cat, /state/f]}, periodSeconds: 2}
  volumes: [{name: state, persistentVolumeClaim: {claimName: state}}]
YAML
)

try_config() {
  local name="$1"
  shift
  local dir="/var/lib/k3s-spike-$name"
  ts "== attempt $name: $*"
  $SUDO mkdir -p "$dir/agent/images" && $SUDO cp "$work/airgap.tar.zst" "$dir/agent/images/"
  local start=$(date +%s)
  $SUDO "$work/k3s" server --data-dir "$dir" --write-kubeconfig "$work/kubeconfig-$name" --write-kubeconfig-mode 644 \
    --disable traefik,metrics-server --node-name spike "$@" >"$OUT/k3s-$name.log" 2>&1 &
  local kc="$work/kubeconfig-$name" k="$work/k3s kubectl --kubeconfig $work/kubeconfig-$name"
  for _ in $(seq 120); do
    [ -f "$kc" ] && $k get node spike >/dev/null 2>&1 && break
    sleep 1
  done
  if ! $k wait --for=condition=Ready node/spike --timeout=120s; then
    ts "RESULT $name: node not Ready"
    tail -40 "$OUT/k3s-$name.log"
    cleanup_k3s
    return 1
  fi
  ts "$name: node Ready after $(($(date +%s) - start))s"
  local busybox=""
  for _ in $(seq 30); do
    busybox=$($k get node spike -o jsonpath='{range .status.images[*]}{.names[0]}{"\n"}{end}' | grep -m1 busybox)
    [ -n "$busybox" ] && break
    sleep 2
  done
  echo "busybox image: $busybox"
  echo "${POD_YAML//BUSYBOX/$busybox}" | $k apply -f -
  if ! $k wait --for=condition=Ready pod/server --timeout=180s; then
    ts "RESULT $name: pod not Ready"
    $k describe pod server | tail -30
    $k get pods -A -o wide
    $k get events -A | tail -30
    tail -40 "$OUT/k3s-$name.log"
    cleanup_k3s
    return 1
  fi
  ts "$name: pod Ready after $(($(date +%s) - start))s"
  local ip
  ip=$($k get pod server -o jsonpath='{.status.podIP}')
  $k run client --image="$busybox" --restart=Never --command -- wget -qO- "http://$ip:8080/" >/dev/null
  $k wait --for=jsonpath='{.status.phase}'=Succeeded pod/client --timeout=90s
  local got
  got=$($k logs client 2>&1)
  $k exec server -- id
  $k expose pod server --port=8080 --name=server
  $k run svc-client --image="$busybox" --restart=Never --command -- wget -T 10 -qO- http://server.default.svc.cluster.local:8080/
  if $k wait --for=jsonpath='{.status.phase}'=Succeeded pod/svc-client --timeout=60s; then
    echo "service via DNS: $($k logs svc-client)"
  else
    echo "service via DNS: FAILED $($k logs svc-client 2>&1 | tail -3)"
  fi
  $k get pods -A
  ts "RESULT $name: OK pod-to-pod='$got' total=$(($(date +%s) - start))s"
  cleanup_k3s
  return 0
}

fake_kernel_sysctls
ok=""
# iptables mode: the kernel has no xt_mark, so kube-proxy's and flannel's rule syncs fail.
try_config iptables --flannel-backend=host-gw --disable-network-policy && ok="$ok iptables"
try_config nftables --flannel-backend=host-gw --disable-network-policy --kube-proxy-arg=proxy-mode=nftables && ok="$ok nftables"
ts "== working configs:${ok:- none}"
[ -n "$ok" ]
