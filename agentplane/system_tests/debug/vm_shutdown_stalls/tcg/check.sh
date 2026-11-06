#!/bin/sh
# Check the sequence in one raw serial stream and the corresponding QEMU monitor.
set -eu
serial=$1
monitor=$2
awk '
  {
    gsub(/\r/, "")
    if ($0 == "AGENTPLANE_GUEST_SHUTDOWN_REQUESTED" && stage == 0) stage = 1
    if ($0 == "AGENTPLANE_GUEST_SHUTDOWN_COMPLETE" && stage == 1) stage = 2
    if (stage == 2 && /reboot: Power down$/) complete = 1
  }
  END { exit !complete }
' "$serial"
grep -Fq 'kvm support: disabled' "$monitor"
grep -Fq 'VM status: paused (shutdown)' "$monitor"
