#!/bin/bash
# Boot the Android 14 (API 34) arm64-v8a emulator under pure TCG.
#
# There is no /dev/kvm and no vmx/svm here (the container is itself a
# Firecracker microVM), and the guest is aarch64 on an x86_64 host anyway, so
# acceleration is impossible in both directions.
#
# Deviation from the normal `emulator -avd ...` invocation: the generic
# launcher refuses with "Avd's CPU Architecture 'arm64' is not supported by
# the QEMU2 emulator on x86_64 host", but the linux-x86_64 package still ships
# qemu/linux-x86_64/qemu-system-aarch64, which is itself a full emulator
# launcher accepting -avd. Calling it directly skips that host-arch check and
# runs the arm64 guest under TCG. It needs the package's own lib64 on
# LD_LIBRARY_PATH (libtcmalloc_minimal.so.4).
#
# SELinux is left at the emulator default (enforcing) -- that is the whole
# point of using a Google system image rather than android-x86.
set -euo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
export ANDROID_SDK_ROOT="$S/sdk"
export ANDROID_HOME="$S/sdk"
export ANDROID_AVD_HOME="$S/avd"
export ANDROID_EMULATOR_HOME="$S/avd-home"
export LD_LIBRARY_PATH="$S/sdk/emulator/lib64:$S/sdk/emulator/lib64/qt/lib:$S/sdk/emulator/lib64/gles_swiftshader:${LD_LIBRARY_PATH:-}"
# This binary still initializes Qt even under -no-window, and there is no X
# display here, so it aborts with "no Qt platform plugin could be initialized".
export QT_QPA_PLATFORM=offscreen
mkdir -p "$ANDROID_EMULATOR_HOME"

# Gotcha: without -crash-report-mode disabled the emulator opens a Qt consent
# dialog at startup and blocks forever with the guest CPU at 0% -- it looks
# exactly like a very slow boot. -no-qt avoids the windowing system entirely;
# the -headless binary has no Qt linked in at all.
exec "$S/sdk/emulator/qemu/linux-x86_64/qemu-system-aarch64-headless" -avd nod14 \
  -no-window \
  -show-kernel \
  -no-qt \
  -no-metrics \
  -crash-report-mode disabled \
  -gpu off \
  -no-audio \
  -no-boot-anim \
  -no-snapshot \
  -accel off \
  -memory 4096 \
  -cores 4 \
  ${WRITABLE_SYSTEM:- } \
  -verbose \
  "$@"
