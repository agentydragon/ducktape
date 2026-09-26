#!/usr/bin/env bash
# Run from the repo devshell after stopping any previous download writer.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=${1:?new absolute output directory}
[[ $output == /* && ! -e $output ]]
unit=wyrm2-qwen38-serial-queue
if systemctl --user is-active --quiet "$unit"; then
  echo "Queue already active" >&2
  exit 1
fi
# A previous manually started download must be stopped before resuming its files.
if pgrep -af 'curl.*--output /var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/'; then
  echo "Existing model download writer; stop that owned process before launch" >&2
  exit 1
fi
for command in docker kubectl jq rg curl flock setsid nvidia-smi; do command -v "$command"; done
mkdir -p "$output/source"
cp -R "$here" "$here/../2026-09-26_qwen38_capacity" "$output/source/"
git -C "$here" rev-parse HEAD >"$output/source-revision.txt"
find "$output/source" -type f -exec sha256sum {} + >"$output/source-sha256.txt"
systemd-run --user --unit="$unit" --collect \
  --property=RuntimeMaxSec=48h --property=TimeoutStopSec=90 \
  --property=KillMode=mixed --property=Nice=10 --property=IOSchedulingClass=idle \
  --setenv="PATH=$PATH" \
  "$(command -v bash)" "$output/source/2026-09-26_qwen38_queue/queue.sh" "$output"
