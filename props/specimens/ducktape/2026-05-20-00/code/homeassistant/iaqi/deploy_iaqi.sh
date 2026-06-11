#!/usr/bin/env bash
# Deploy IAQI custom component to 15 Leroy Home Assistant instance from home network.

set -euo pipefail

echo "Deploying IAQI…"
# rsync options:
# -a : archive mode (preserves permissions, times, symlinks…)
# -v : verbose
# -h : human-readable numbers
# -z : compress data during transfer
# -P : equivalent to --partial --progress (shows per-file progress)
# --delete : remove files on the remote side that no longer exist locally
rsync -avzhP --delete "custom_components/indoor_aqi" "ha-15leroy:/root/homeassistant/custom_components/"

echo -e "\nRestarting Home Assistant…"
ssh ha-15leroy "ha core restart"

echo -e "\n✅ Deployment finished, Home Assistant restarted."
