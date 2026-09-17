#!/bin/bash
# Boot Android-x86 9.0-r2 (x86_64, Linux 4.19, real AOSP SELinux policy) under
# QEMU TCG. There is no /dev/kvm and no vmx/svm in this container -- it is
# itself a Firecracker microVM -- so this is pure software emulation.
#
# The kernel and initrd are the ISO's own, extracted; the initrd carries an
# injected /scripts/5-custom (see ../initrd/scripts/5-custom) that turns on
# adb-over-TCP. Booting -kernel/-initrd rather than the ISO bootloader is what
# lets us set the kernel cmdline, most importantly androidboot.selinux and a
# serial console.
set -euo pipefail

S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
SELINUX=${SELINUX:-enforcing}
MEM=${MEM:-6144}
CPUS=${CPUS:-4}

rm -f "$S/vm/serial.sock" "$S/vm/qmp.sock"

# SRC= and DATA= are read as shell variables by the android-x86 initrd's
# /init, not by the kernel. DATA=vda puts /data on the persistent ext4 disk
# instead of the live-CD tmpfs.
CMDLINE="root=/dev/ram0 console=ttyS0,115200n8 androidboot.console=ttyS0"
CMDLINE="$CMDLINE androidboot.selinux=$SELINUX androidboot.hardware=android_x86_64"
CMDLINE="$CMDLINE SRC= DATA=vda SETUPWIZARD=0 HWACCEL=0 nomodeset"

exec qemu-system-x86_64 \
  -machine pc,accel=tcg \
  -cpu max \
  -smp "$CPUS" \
  -m "$MEM" \
  -kernel "$S/iso/kernel" \
  -initrd "$S/vm/initrd-custom.img" \
  -append "$CMDLINE" \
  -drive file="$S/img/android-x86_64-9.0-r2.iso",if=ide,media=cdrom,format=raw \
  -drive file="$S/vm/data.img",if=virtio,format=raw,cache=unsafe \
  -drive file="$S/vm/payload.img",if=virtio,format=raw,cache=unsafe \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:5555-:5555 \
  -device e1000,netdev=net0 \
  -chardev socket,id=ser0,path="$S/vm/serial.sock",server=on,wait=off,logfile="$S/vm/console.log",logappend=on \
  -serial chardev:ser0 \
  -qmp unix:"$S/vm/qmp.sock",server=on,wait=off \
  -display none \
  -no-reboot
