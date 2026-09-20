#!/bin/sh
set -e

if [ "$#" -ne 2 ]; then
  echo "usage: $0 IMAGE_URL INSTALL_DISK < NETRC_CONTENT" >&2
  exit 2
fi

image_url=$1
install_disk=$2
netrc=/tmp/talos-image-factory.netrc
installer_script=$0

# Terraform provides the netrc entry on stdin, keeping credentials out of
# argv. Create it privately and remove both temporary files on every exit.
umask 077
trap 'rm -f "$netrc" "$installer_script"' EXIT
cat >"$netrc"
chmod 0600 "$netrc"
test -s "$netrc"

case "$install_disk" in
  /dev/*) ;;
  *)
    echo "install disk must be a /dev path: $install_disk" >&2
    exit 2
    ;;
esac

# OVH rescue runs dash (no pipefail). Decompress to a temp file and dd from
# that, so an unrelated decompressor failure can't silently feed dd zero
# bytes. KS-5 has 32 GB RAM; /tmp on tmpfs has room for the ~1.5 GB raw image.
# xz-utils is preinstalled; install zstd for current Factory images.
apt-get update -qq && apt-get install -y -qq zstd

wget --netrc-file="$netrc" -q -O /tmp/talos.bin "$image_url"
case "$image_url" in
  *.zst) zstd -dc /tmp/talos.bin >/tmp/talos.raw ;;
  *.xz) xz -dc /tmp/talos.bin >/tmp/talos.raw ;;
  *)
    echo "unknown compression in $image_url" >&2
    exit 1
    ;;
esac

# Do not let a failed decompressor silently feed dd zero bytes to the disk.
test -s /tmp/talos.raw
dd if=/tmp/talos.raw of="$install_disk" bs=4M status=progress
sync
