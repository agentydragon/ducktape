#!/usr/bin/env bash
set -euo pipefail

HA_HOST=homeassistant
HA_PACKAGES_DIR=/config/packages/rai
LOCAL_DIR="$(cd "$(dirname "$0")/packages/rai" && pwd)"

changed_files=$(rsync -avn --out-format="%n" "$LOCAL_DIR/" "$HA_HOST:$HA_PACKAGES_DIR/" | grep -v '/$' | grep '\.' || true)

if [[ -z "$changed_files" ]]; then
  echo "No differences — HA is up to date."
  exit 0
fi

echo "=== Diff: local vs HA ==="
while IFS= read -r file; do
  diff <(ssh "$HA_HOST" "cat '$HA_PACKAGES_DIR/$file' 2>/dev/null || true") "$LOCAL_DIR/$file" \
    --label "HA:$file" --label "local:$file" -u || true
done <<<"$changed_files"

echo
read -rp "Deploy? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
  rsync -av "$LOCAL_DIR/" "$HA_HOST:$HA_PACKAGES_DIR/"
  echo "=== Reloading HA ==="
  ssh "$HA_HOST" 'ha core restart'
  echo "Done."
else
  echo "Aborted."
fi
