#!/bin/sh
set -eu

QEMU=""
for candidate in /scratch/qemu-kvm /usr/bin/qemu-kvm /usr/libexec/qemu-kvm /usr/bin/qemu-system-x86_64; do
  if [ -x "$candidate" ]; then
    QEMU="$candidate"
    break
  fi
done
[ -n "$QEMU" ] || {
  echo "QEMU_BINARY_NOT_FOUND"
  exit 10
}
VIRTIOFSD=/usr/libexec/virtiofsd
[ -x "$VIRTIOFSD" ] || {
  echo "VIRTIOFSD_BINARY_NOT_FOUND"
  exit 11
}

echo "QEMU_BINARY=$QEMU"
"$QEMU" --version
echo "QEMU_ARGS: -machine q35,accel=tcg"
if test -e /dev/kvm; then echo "KVM_DEVICE=present"; else echo "KVM_DEVICE=absent"; fi
grep '^CapEff:' /proc/self/status
sha256sum -c /scratch/input.sha256

mkdir -p /scratch/state2
rm -f /tmp/virtiofs-state.sock /scratch/virtiofsd.log /scratch/qemu-monitor.log /scratch/guest-serial.log
"$VIRTIOFSD" --version 2>&1 || true
"$VIRTIOFSD" \
  --socket-path=/tmp/virtiofs-state.sock \
  --shared-dir=/scratch/state2 \
  --sandbox=none \
  --cache=auto >/scratch/virtiofsd.log 2>&1 &
VIRTIOFSD_PID=$!
cleanup_virtiofsd() {
  kill "$VIRTIOFSD_PID" 2>/dev/null || true
  wait "$VIRTIOFSD_PID" 2>/dev/null || true
}
trap cleanup_virtiofsd EXIT HUP INT TERM
count=0
while [ ! -S /tmp/virtiofs-state.sock ] && [ "$count" -lt 40 ]; do
  kill -0 "$VIRTIOFSD_PID" 2>/dev/null || {
    cat /scratch/virtiofsd.log
    exit 12
  }
  sleep 0.25
  count=$((count + 1))
done
[ -S /tmp/virtiofs-state.sock ] || {
  cat /scratch/virtiofsd.log
  exit 13
}
echo "VIRTIOFSD_SOCKET=ready"
grep -E '^(Uid|Gid|CapEff):' "/proc/$VIRTIOFSD_PID/status"
# The guest consumes the exact CI vmlinuz, initramfs, root erofs and runner erofs.
# Workspace and ISO disks are disposable placeholders; there is no Agentplane service stack.
# This only probes TCG guest boot/ACPI plumbing, not reproduction of the shutdown stall.
(
  sleep 60
  printf 'info status\ninfo kvm\ninfo cpus\nsystem_powerdown\n'
  sleep 30
  printf 'info status\nquit\n'
) | "$QEMU" \
  -name agentplane-vm-tcg-smoke \
  -L /usr/share/qemu-kvm \
  -bios /usr/share/seabios/bios-256k.bin \
  -nodefaults \
  -object memory-backend-memfd,id=mem,size=1536M,share=on \
  -machine q35,accel=tcg \
  -numa node,memdev=mem \
  -m 1536M \
  -smp 2 \
  -display none \
  -no-reboot \
  -no-shutdown \
  -kernel /scratch/vmlinuz \
  -initrd /scratch/initramfs.cpio \
  -append 'console=ttyS0 net.ifnames=0' \
  -drive file=/scratch/root.img,if=none,id=guest-root,format=raw,readonly=on \
  -device virtio-blk-pci,drive=guest-root,serial=guest-root \
  -drive file=/scratch/test-runner.img,if=none,id=runner,format=raw,readonly=on \
  -device virtio-blk-pci,drive=runner,serial=agentplane-runner \
  -drive file=/scratch/workspace.img,if=none,id=workspace,format=raw \
  -device virtio-blk-pci,drive=workspace,serial=workspace \
  -drive file=/scratch/empty.iso,if=none,id=config,format=raw,readonly=on \
  -device virtio-blk-pci,drive=config,serial=agentplane-config \
  -drive file=/scratch/empty.iso,if=none,id=trust,format=raw,readonly=on \
  -device virtio-blk-pci,drive=trust,serial=agentplane-trust \
  -drive file=/scratch/empty.iso,if=none,id=launch,format=raw,readonly=on \
  -device virtio-blk-pci,drive=launch,serial=agentplane-launch \
  -netdev user,id=guestnet \
  -device virtio-net-pci,netdev=guestnet,romfile= \
  -chardev socket,id=char-state,path=/tmp/virtiofs-state.sock \
  -device vhost-user-fs-pci,chardev=char-state,tag=state \
  -monitor stdio \
  -serial file:/scratch/guest-serial.log \
  >/scratch/qemu-monitor.log 2>&1
qemu_rc=$?
cleanup_virtiofsd
trap - EXIT HUP INT TERM
echo "QEMU_EXIT=$qemu_rc"
echo '--- QEMU monitor ---'
cat /scratch/qemu-monitor.log
echo '--- Guest serial ---'
cat /scratch/guest-serial.log
echo '--- virtiofsd log ---'
cat /scratch/virtiofsd.log
