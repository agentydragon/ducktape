#!/bin/bash
set -Eeuo pipefail
umask 022

readonly FIRECRACKER_VERSION=v1.17.0
readonly FIRECRACKER_ARCHIVE="firecracker-${FIRECRACKER_VERSION}-x86_64.tgz"
readonly FIRECRACKER_SHA256=06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558
readonly FIRECRACKER_COMMIT=95f868c
readonly KERNEL_VERSION=6.1.188
readonly KERNEL_ARCHIVE="linux-${KERNEL_VERSION}.tar.xz"
readonly KERNEL_SHA256=ed4d0acb1307c235230c89efc094e210e6290593f94a7e617f28b1001101a33a
readonly FIRECRACKER_BASE_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}"
readonly FIRECRACKER_RAW_URL="https://raw.githubusercontent.com/firecracker-microvm/firecracker/${FIRECRACKER_COMMIT}"
readonly KERNEL_BASE_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x"
readonly UBUNTU_ROOTFS_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261028-84df49062576-0/x86_64/ubuntu-24.04.squashfs"
readonly UBUNTU_ROOTFS_SHA256=568127fd53c728c1f275fbbcc0d61da065b435e455b89307e985b32b02dc56d9
readonly KERNEL_CONFIG_SHA256=153ca1b40f3312bfb40b7587b471ea548a915f98f480f0d0892a1537957a2147

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORK=${WORK:-/work}
SRC=${SPIKE_SRC:-"$WORK/src"}
ARTIFACTS="$WORK/artifacts"
DOWNLOADS="$WORK/downloads"
BUILD="$WORK/build"
JOBS=${JOBS:-4}

die() {
  printf 'prepare: ERROR: %s\n' "$*" >&2
  exit 1
}

note() {
  printf 'prepare: %s\n' "$*"
}

require_commands() {
  local command
  for command in curl sha256sum tar xz unsquashfs mke2fs mkfs.ext4 mkfs.erofs \
    xorriso make gcc flex bison bc cpio perl python3; do
    command -v "$command" >/dev/null || die "required command is missing: $command"
  done
}

download() {
  local url=$1
  local destination=$2
  if [[ -s $destination ]]; then
    return
  fi
  mkdir -p "$(dirname -- "$destination")"
  rm -f "$destination.part"
  curl --fail --location --retry 4 --retry-all-errors --connect-timeout 20 \
    --output "$destination.part" "$url"
  [[ -s $destination.part ]] || die "download was empty: $url"
  mv "$destination.part" "$destination"
}

verify_sha256() {
  local expected=$1
  local file=$2
  local actual
  actual=$(sha256sum "$file" | cut -d ' ' -f 1)
  [[ $actual == "$expected" ]] || die "SHA-256 mismatch for $file: expected $expected, got $actual"
  note "verified SHA-256 $actual  $file"
}

create_blank_ext4() {
  local destination=$1
  local size=$2
  local label=$3
  local tmp="${destination}.tmp"
  if [[ -e $destination ]]; then
    local actual_type
    actual_type=$(blkid -p -s TYPE -o value "$destination" 2>/dev/null || true)
    [[ $actual_type == ext4 ]] || die "existing $destination is not ext4; preserving it"
    note "preserving existing ext4 image $destination"
    return
  fi
  rm -f "$tmp"
  truncate -s "$size" "$tmp"
  mkfs.ext4 -F -q -m 0 -L "$label" "$tmp"
  mv "$tmp" "$destination"
}

create_ext4_from_tree() {
  local destination=$1
  local size=$2
  local label=$3
  local source=$4
  local tmp="${destination}.tmp"
  if [[ -e $destination ]]; then
    local actual_type
    actual_type=$(blkid -p -s TYPE -o value "$destination" 2>/dev/null || true)
    [[ $actual_type == ext4 ]] || die "existing $destination is not ext4; preserving it"
    note "preserving existing ext4 image $destination"
    return
  fi
  rm -f "$tmp"
  truncate -s "$size" "$tmp"
  mkfs.ext4 -F -q -m 0 -L "$label" -d "$source" "$tmp"
  mv "$tmp" "$destination"
}

create_iso_from_tree() {
  local destination=$1
  local volume_id=$2
  local source=$3
  local tmp="${destination}.tmp"
  if [[ -e $destination ]]; then
    local actual_type
    actual_type=$(blkid -p -s TYPE -o value "$destination" 2>/dev/null || true)
    [[ $actual_type == iso9660 ]] || die "existing $destination is not ISO9660; preserving it"
    note "preserving existing ISO image $destination"
    return
  fi
  rm -f "$tmp"
  xorriso -as mkisofs -quiet -R -J -V "$volume_id" -o "$tmp" "$source"
  mv "$tmp" "$destination"
}

prepare_firecracker() {
  local archive="$DOWNLOADS/$FIRECRACKER_ARCHIVE"
  local checksum="$DOWNLOADS/${FIRECRACKER_ARCHIVE}.sha256.txt"
  download "$FIRECRACKER_BASE_URL/$FIRECRACKER_ARCHIVE" "$archive"
  download "$FIRECRACKER_BASE_URL/${FIRECRACKER_ARCHIVE}.sha256.txt" "$checksum"

  local sidecar_sha
  sidecar_sha=$(awk -v name="$FIRECRACKER_ARCHIVE" '$2 == name {print $1}' "$checksum")
  [[ $sidecar_sha == "$FIRECRACKER_SHA256" ]] \
    || die "Firecracker release checksum sidecar changed or is malformed: $checksum"
  verify_sha256 "$FIRECRACKER_SHA256" "$archive"

  local extracted="$BUILD/firecracker-release"
  mkdir -p "$extracted"
  if [[ ! -s $ARTIFACTS/firecracker ]]; then
    tar -xzf "$archive" -C "$extracted"
    local binary
    binary=$(find "$extracted" -type f -name "firecracker-${FIRECRACKER_VERSION}-x86_64" -print -quit)
    [[ -n $binary ]] || die "Firecracker binary was not found in $archive"
    install -m 0755 "$binary" "$ARTIFACTS/firecracker.tmp"
    mv "$ARTIFACTS/firecracker.tmp" "$ARTIFACTS/firecracker"
  fi
}

prepare_kernel() {
  local source_archive="$DOWNLOADS/$KERNEL_ARCHIVE"
  local sums="$DOWNLOADS/kernel-sha256sums.asc"
  local config="$DOWNLOADS/microvm-kernel-ci-x86_64-6.1.config"
  local source="$BUILD/linux-$KERNEL_VERSION"
  local output="$BUILD/linux-$KERNEL_VERSION-build"
  download "$KERNEL_BASE_URL/$KERNEL_ARCHIVE" "$source_archive"
  download "$KERNEL_BASE_URL/sha256sums.asc" "$sums"
  download "$FIRECRACKER_RAW_URL/resources/guest_configs/microvm-kernel-ci-x86_64-6.1.config" "$config"
  verify_sha256 "$KERNEL_CONFIG_SHA256" "$config"

  local manifest_sha
  manifest_sha=$(awk -v name="$KERNEL_ARCHIVE" '$2 == name {print $1}' "$sums")
  [[ $manifest_sha == "$KERNEL_SHA256" ]] \
    || die "kernel checksum does not match the pinned official manifest entry: $sums"
  verify_sha256 "$KERNEL_SHA256" "$source_archive"

  if [[ ! -f $source/Makefile ]]; then
    local unpacked="$BUILD/linux-$KERNEL_VERSION.extracting"
    rm -rf "$unpacked"
    mkdir -p "$unpacked"
    tar -xJf "$source_archive" -C "$unpacked" --strip-components=1
    [[ -f $unpacked/Makefile ]] || die "kernel source extraction is incomplete"
    rm -rf "$source"
    mv "$unpacked" "$source"
  fi

  mkdir -p "$output"
  if [[ ! -s $ARTIFACTS/vmlinux ]]; then
    if [[ ! -f $output/.config ]]; then
      install -m 0644 "$config" "$output/.config"
    fi
    "$source/scripts/config" --file "$output/.config" \
      --enable EROFS_FS \
      --enable EROFS_FS_XATTR \
      --enable EROFS_FS_POSIX_ACL \
      --enable ISO9660_FS \
      --enable EXT4_FS \
      --enable EXT4_FS_POSIX_ACL \
      --enable EXT4_FS_SECURITY \
      --enable FS_POSIX_ACL \
      --enable VIRTIO \
      --enable VIRTIO_MMIO \
      --enable VIRTIO_BLK \
      --enable VIRTIO_NET \
      --enable IPV6 \
      --enable DEVTMPFS \
      --enable PROC_FS \
      --enable SYSFS \
      --enable TMPFS \
      --enable SERIAL_8250 \
      --enable SERIAL_8250_CONSOLE
    make -C "$source" O="$output" ARCH=x86_64 olddefconfig

    local required_config
    for required_config in \
      CONFIG_EROFS_FS=y \
      CONFIG_EROFS_FS_POSIX_ACL=y \
      CONFIG_ISO9660_FS=y \
      CONFIG_EXT4_FS=y \
      CONFIG_EXT4_FS_POSIX_ACL=y \
      CONFIG_EXT4_FS_SECURITY=y \
      CONFIG_FS_POSIX_ACL=y \
      CONFIG_VIRTIO_MMIO=y \
      CONFIG_VIRTIO_BLK=y \
      CONFIG_VIRTIO_NET=y \
      CONFIG_IPV6=y \
      CONFIG_DEVTMPFS=y \
      CONFIG_PROC_FS=y \
      CONFIG_SYSFS=y \
      CONFIG_TMPFS=y \
      CONFIG_SERIAL_8250_CONSOLE=y; do
      grep -Fxq "$required_config" "$output/.config" \
        || die "kernel configuration did not enable $required_config"
    done

    note "building Linux $KERNEL_VERSION with $JOBS jobs"
    make -C "$source" O="$output" ARCH=x86_64 -j"$JOBS" vmlinux
    [[ -s $output/vmlinux ]] || die 'Linux build completed without vmlinux'
    install -m 0644 "$output/vmlinux" "$ARTIFACTS/vmlinux.tmp"
    mv "$ARTIFACTS/vmlinux.tmp" "$ARTIFACTS/vmlinux"
  else
    note "preserving existing kernel image $ARTIFACTS/vmlinux"
  fi
}

prepare_rootfs() {
  local image="$ARTIFACTS/rootfs.ext4"
  if [[ -e $image ]]; then
    local actual_type
    actual_type=$(blkid -p -s TYPE -o value "$image" 2>/dev/null || true)
    [[ $actual_type == ext4 ]] || die "existing $image is not ext4; preserving it"
    note "preserving existing root filesystem $image"
    return
  fi

  local squashfs="$DOWNLOADS/ubuntu-24.04.squashfs"
  download "$UBUNTU_ROOTFS_URL" "$squashfs"
  verify_sha256 "$UBUNTU_ROOTFS_SHA256" "$squashfs"
  local tree="$BUILD/ubuntu-24.04-rootfs"
  if [[ ! -d $tree ]]; then
    local unpacked="$BUILD/ubuntu-24.04-rootfs.extracting"
    rm -rf "$unpacked"
    unsquashfs -no-progress -d "$unpacked" "$squashfs"
    [[ -x $unpacked/bin/sh ]] || die 'Ubuntu rootfs extraction is incomplete'
    mv "$unpacked" "$tree"
  fi

  local guest_python="$SRC/guest.py"
  [[ -f $guest_python ]] || die "missing $guest_python; provide the standalone guest server before building rootfs.ext4"
  install -D -m 0755 "$SCRIPT_DIR/guest-init.sh" "$tree/sbin/spike-init"
  install -D -m 0644 "$guest_python" "$tree/opt/spike/guest.py"
  mkdir -p "$tree/run/agentplane" "$tree/state/workspaces"
  create_ext4_from_tree "$image" 1G spike-root "$tree"
}

prepare_runner_disk() {
  if [[ ! -e $ARTIFACTS/runner.erofs ]]; then
    [[ -s $ARTIFACTS/agentplane-inputs.tar ]] || die 'build and stage the agentplane_inputs Bazel target first'
    tar -xf "$ARTIFACTS/agentplane-inputs.tar" -C "$ARTIFACTS"
    mv "$ARTIFACTS/runner.img" "$ARTIFACTS/runner.erofs"
  else
    [[ -s $ARTIFACTS/runner.erofs ]] || die "existing runner image is empty; preserving $ARTIFACTS/runner.erofs"
    note "preserving existing runner image $ARTIFACTS/runner.erofs"
  fi
}

prepare_config_disks() {
  local config_tree="$BUILD/config-root"
  local launch_tree="$BUILD/launch-root"
  local trust_tree="$BUILD/trust-root"
  mkdir -p "$config_tree" "$launch_tree" "$trust_tree"

  if [[ ! -e $ARTIFACTS/config.iso ]]; then
    if [[ -f $SRC/config/environment-runtime.json ]]; then
      install -m 0644 "$SRC/config/environment-runtime.json" "$config_tree/environment-runtime.json"
    else
      printf '{}\n' >"$config_tree/environment-runtime.json"
    fi
    if [[ -f $SRC/config/environment-runtime.env ]]; then
      install -m 0644 "$SRC/config/environment-runtime.env" "$config_tree/environment-runtime.env"
    else
      : >"$config_tree/environment-runtime.env"
    fi
    create_iso_from_tree "$ARTIFACTS/config.iso" AGENTPLANE_CONFIG "$config_tree"
  fi

  if [[ ! -e $ARTIFACTS/launch.iso ]]; then
    if [[ -f $SRC/launch/launch.json ]]; then
      install -m 0644 "$SRC/launch/launch.json" "$launch_tree/launch.json"
    else
      printf '{}\n' >"$launch_tree/launch.json"
    fi
    create_iso_from_tree "$ARTIFACTS/launch.iso" AGENTPLANE_LAUNCH "$launch_tree"
  fi

  if [[ ! -e $ARTIFACTS/trust.iso ]]; then
    [[ -f /etc/ssl/certs/ca-certificates.crt ]] || die 'host CA bundle is missing'
    install -m 0644 /etc/ssl/certs/ca-certificates.crt "$trust_tree/ca-certificates.crt"
    create_iso_from_tree "$ARTIFACTS/trust.iso" AGENTPLANE_TRUST "$trust_tree"
  fi
}

prepare_state_disks() {
  create_blank_ext4 "$ARTIFACTS/state.ext4" 512M spike-state
  create_blank_ext4 "$ARTIFACTS/workspace.ext4" 1G spike-workspace
}

prepare_credentials_disk() {
  local image="$ARTIFACTS/credentials.ext4"
  local tree="$BUILD/credentials-root"
  if [[ -e $image ]]; then
    local actual_type
    actual_type=$(blkid -p -s TYPE -o value "$image" 2>/dev/null || true)
    [[ $actual_type == ext4 ]] || die "existing $image is not ext4; preserving it"
    note "preserving existing credentials image $image"
    return
  fi

  rm -rf "$tree"
  mkdir -m 0700 -p "$tree"
  if [[ -d $SRC/credentials ]]; then
    cp -a "$SRC/credentials/." "$tree/"
  fi
  if [[ ! -e $tree/tls.key ]]; then
    printf 'firecracker-spike-dummy-tls-key\n' >"$tree/tls.key"
  fi
  find "$tree" -type d -exec chmod 0700 {} +
  find "$tree" -type f -exec chmod 0600 {} +
  create_ext4_from_tree "$image" 64M spike-creds "$tree"
}

write_hashes() {
  local hash_file="$ARTIFACTS/input-hashes.txt"
  {
    printf 'Firecracker release: %s\n' "$FIRECRACKER_BASE_URL/$FIRECRACKER_ARCHIVE"
    sha256sum "$DOWNLOADS/$FIRECRACKER_ARCHIVE" "$DOWNLOADS/${FIRECRACKER_ARCHIVE}.sha256.txt"
    printf '\nLinux source: %s/%s\n' "$KERNEL_BASE_URL" "$KERNEL_ARCHIVE"
    sha256sum "$DOWNLOADS/$KERNEL_ARCHIVE" "$DOWNLOADS/kernel-sha256sums.asc"
    printf '\nFirecracker guest config at commit %s\n' "$FIRECRACKER_COMMIT"
    sha256sum "$DOWNLOADS/microvm-kernel-ci-x86_64-6.1.config"
    printf '\nUbuntu 24.04 guest rootfs: %s\n' "$UBUNTU_ROOTFS_URL"
    sha256sum "$DOWNLOADS/ubuntu-24.04.squashfs"
    printf '\nSpike inputs\n'
    sha256sum "$SCRIPT_DIR/prepare.sh" "$SCRIPT_DIR/guest-init.sh" "$SRC/guest.py"
    for input in \
      "$SRC/config/environment-runtime.json" \
      "$SRC/config/environment-runtime.env" \
      "$SRC/launch/launch.json"; do
      if [[ -f $input ]]; then
        sha256sum "$input"
      fi
    done
  } | sed "s|$WORK/||g" >"$hash_file"

  (
    cd "$ARTIFACTS"
    sha256sum firecracker vmlinux rootfs.ext4 runner.erofs state.ext4 \
      workspace.ext4 config.iso launch.iso trust.iso credentials.ext4 usr/local/bin/egress-sidecar \
      >SHA256SUMS
  )
}

main() {
  [[ $(id -u) -eq 0 ]] || die 'run as root inside the privileged launcher Pod'
  require_commands
  mkdir -p "$ARTIFACTS" "$DOWNLOADS" "$BUILD"

  note 'preparing the pinned Firecracker release'
  prepare_firecracker
  note "preparing Linux $KERNEL_VERSION"
  prepare_kernel
  note 'preparing guest rootfs and attached disks'
  prepare_rootfs
  prepare_runner_disk
  prepare_config_disks
  prepare_state_disks
  prepare_credentials_disk
  write_hashes
  note "all spike artifacts are ready under $ARTIFACTS"
}

main "$@"
