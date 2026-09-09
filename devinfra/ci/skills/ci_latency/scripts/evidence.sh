#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: $0 SNAPSHOT_DIRECTORY" >&2
  exit 2
fi
out=$1
scripts=$(cd "$(dirname "$0")" && pwd)
mapfile -t window <"$out/window.txt"
for recipe in summarize contention; do
  jq --arg since "${window[0]}" --arg until "${window[1]}" -f "$scripts/$recipe.jq" \
    "$out/snapshot.json" >"$out/$recipe.json"
done
jq -s -f "$scripts/checks.jq" "$out"/checks/*.json >"$out/check_summary.json"
jq -n --slurpfile summary "$out/summarize.json" --slurpfile contention "$out/contention.json" \
  --slurpfile checks "$out/check_summary.json" \
  '{summary:($summary[0]|del(.steps)),contention:$contention[0],checks:$checks[0]}'
