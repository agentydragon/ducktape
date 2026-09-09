#!/usr/bin/env bash
# Collect public Actions metadata. No logs or credentials are written to the snapshot.
set -euo pipefail
if [[ $# != 4 ]]; then
  echo "Usage: $0 OWNER/REPO SINCE_UTC UNTIL_UTC NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
repo=$1
since=$2
until=$3
out=$4
mkdir "$out"
mkdir "$out/jobs" "$out/checks"
date -u +%FT%TZ >"$out/collection_started_at.txt"
git rev-parse HEAD >"$out/source_commit.txt"
printf '%s\n' "$repo" >"$out/repository.txt"
printf '%s\n%s\n' "$since" "$until" >"$out/window.txt"
# Filtered Actions searches cap at 1,000; fail rather than silently truncate.
gh api --method GET "repos/$repo/actions/runs" -f "created=$since..$until" -f per_page=100 \
  --paginate --slurp >"$out/run_pages.json"
jq -e '.[0].total_count < 1000' "$out/run_pages.json" >/dev/null
jq '[.[].workflow_runs[]] | unique_by(.id)' "$out/run_pages.json" >"$out/runs.json"
# Include old unfinished runs, which a creation-time window would miss.
for status in queued in_progress pending waiting requested; do
  gh api --method GET "repos/$repo/actions/runs" -f "status=$status" -f per_page=100 \
    --paginate --slurp >"$out/active-$status.json"
  jq -e '.[0].total_count < 1000' "$out/active-$status.json" >/dev/null
done
jq -s '[.[0][], (.[1:][] | .[].workflow_runs[])] | unique_by(.id)' \
  "$out/runs.json" "$out"/active-*.json >"$out/all_runs.json"
# Four concurrent readers keep collection bounded without a request burst.
export repo out
jq -r '.[].id' "$out/all_runs.json" | xargs -P 4 -I '{}' bash -c '
  set -euo pipefail
  gh api "repos/$repo/actions/runs/$1/jobs?filter=all&per_page=100" --paginate --slurp > "$out/jobs/$1.json"
' _ '{}'
gh api "repos/$repo/pulls?state=open&per_page=100" --paginate --slurp >"$out/pulls.json"
jq -r '.[].[] | [.number, .head.sha] | @tsv' "$out/pulls.json" | while IFS=$'\t' read -r number sha; do
  gh api "repos/$repo/commits/$sha/check-runs?filter=latest&per_page=100" --paginate --slurp >"$out/checks/$number.json"
done
date -u +%FT%TZ >"$out/collection_finished_at.txt"
jq -n --slurpfile runs "$out/all_runs.json" \
  --slurpfile pages <(jq -s '.' "$out"/jobs/*.json) \
  --arg collected_at "$(cat "$out/collection_finished_at.txt")" \
  '{collected_at: $collected_at, runs: $runs[0], jobs: [$pages[0][] | .[].jobs[]] | unique_by(.id)}' \
  >"$out/snapshot.json"
echo "Snapshot: $out/snapshot.json"
