#!/usr/bin/env bash
set -euo pipefail

config_dir=/run/buildbuddy-config
install_dir=/usr/local/libexec/buildbuddy-kvm-worker
for path in /buildbuddy /run/buildbuddy-key "$config_dir"; do
  mountpoint --quiet "$path"
done
test -s /run/buildbuddy-key/api-key
test "$(findmnt --noheadings --output FSTYPE --target /buildbuddy)" = ext4

install -Dm0644 "$config_dir/executor.yaml" /etc/buildbuddy/executor.yaml
install -Dm0644 "$config_dir/daemon.json" /etc/docker/daemon.json
install -Dm0644 "$config_dir/docker-seccomp.json" /etc/docker/buildbuddy-seccomp.json
install -Dm0644 "$config_dir/containerd.toml" /etc/containerd/config.toml
install -Dm0644 "$config_dir/containerd-mounts.conf" /etc/systemd/system/containerd.service.d/10-buildbuddy-work.conf
install -Dm0644 "$config_dir/executor.service" /etc/systemd/system/buildbuddy-executor.service
install -Dm0644 "$config_dir/modules-load.conf" /etc/modules-load.d/buildbuddy-kvm-worker.conf
install -Dm0644 "$config_dir/modprobe.conf" /etc/modprobe.d/buildbuddy-kvm-worker.conf
install -Dm0644 "$config_dir/sysctl.conf" /etc/sysctl.d/90-buildbuddy-kvm-worker.conf
install -Dm0644 "$config_dir/k3s.yaml" /etc/rancher/k3s/config.yaml
install -Dm0644 "$config_dir/registries.yaml" /etc/rancher/k3s/registries.yaml
install -Dm0755 "$config_dir/run-executor.sh" "$install_dir/run-executor"
if grep -qw svm /proc/cpuinfo; then
  kvm_module=kvm_amd
elif grep -qw vmx /proc/cpuinfo; then
  kvm_module=kvm_intel
else
  echo "The guest CPU does not expose SVM or VMX" >&2
  exit 1
fi
printf '%s\n' "$kvm_module" >>/etc/modules-load.d/buildbuddy-kvm-worker.conf
modprobe -a br_netfilter bridge ip6_tables ip6table_nat ip_tables iptable_nat \
  "$kvm_module" loop nf_conntrack nf_nat overlay tun vhost_net vhost_vsock vxlan
case "$(cat "/sys/module/$kvm_module/parameters/nested")" in
  1 | Y) ;;
  *)
    echo "Nested KVM is disabled for $kvm_module" >&2
    exit 1
    ;;
esac
udevadm settle
test -c /dev/kvm
test -c /dev/loop-control
test -b /dev/loop0
test -c /dev/net/tun
sysctl -p /etc/sysctl.d/90-buildbuddy-kvm-worker.conf

# Docker's default AppArmor profile denies the mounts K3s needs. This setting
# applies only to this dedicated guest. Cloud-init reboots after finishing setup.
install -d /etc/default/grub.d
cat >/etc/default/grub.d/90-buildbuddy-kvm-worker.cfg <<'GRUB'
GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT:+$GRUB_CMDLINE_LINUX_DEFAULT }apparmor=0 loop.max_loop=64"
GRUB
update-grub

# Administrative console access requires outer-cluster permissions; SSH stays off.
install -d /etc/systemd/system/serial-getty@ttyS0.service.d
cat >/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I 115200,38400,9600 vt102
UNIT
systemctl mask --now ssh.service ssh.socket
systemctl stop docker.service docker.socket containerd.service
systemctl daemon-reload
systemctl restart containerd.service
systemctl enable docker.service
systemctl restart docker.service
apt-get clean
systemctl enable buildbuddy-executor.service
if grep --word-regexp --quiet 'apparmor=0' /proc/cmdline; then
  systemctl start buildbuddy-executor.service
else
  touch /run/reboot-required
fi
