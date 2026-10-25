#!/usr/bin/env bash
set -euo pipefail

if [[ -z ${GIT_REMOTE_URL:-} || -z ${GIT_AUTH_USER:-} || -z ${GIT_AUTH_TOKEN:-} ]]; then
  echo 'GIT_REMOTE_URL, GIT_AUTH_USER, and GIT_AUTH_TOKEN are required' >&2
  exit 2
fi
if [[ $GIT_REMOTE_URL == *'://'*@* || $GIT_REMOTE_URL == *$'\n'* || $GIT_REMOTE_URL == *,* ]]; then
  echo 'GIT_REMOTE_URL must be credential-free and contain no CSV delimiters' >&2
  exit 2
fi
target=${BENCH_TARGET:-}
if [[ ! $target =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo 'BENCH_TARGET must be a short label containing only letters, digits, _ or -' >&2
  exit 2
fi
clone_samples=${CLONE_SAMPLES:-5}
fetch_samples=${FETCH_SAMPLES:-20}
if [[ ! $clone_samples =~ ^[1-9][0-9]*$ || ! $fetch_samples =~ ^[1-9][0-9]*$ ]]; then
  echo 'CLONE_SAMPLES and FETCH_SAMPLES must be positive integers' >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../../../.." && pwd)
evidence_dir=${EVIDENCE_DIR:-/tmp/git-transfer-$target-$(date -u +%Y%m%dT%H%M%SZ)}
case "$evidence_dir" in
  /*) ;;
  *)
    echo 'EVIDENCE_DIR must be an absolute path outside the repository' >&2
    exit 2
    ;;
esac
case "$evidence_dir/" in
  "$repo_root/"*)
    echo 'EVIDENCE_DIR cannot be inside the repository' >&2
    exit 2
    ;;
esac
umask 077
mkdir -p "$evidence_dir"
scratch=$(mktemp -d /tmp/git-transfer-bench.XXXXXX)
trap 'rm -rf "$scratch"' EXIT

auth=$(printf '%s:%s' "$GIT_AUTH_USER" "$GIT_AUTH_TOKEN" | base64 | tr -d '\n')
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic $auth"
unset auth

{
  printf 'target=%s\n' "$target"
  printf 'remote=%s\n' "$GIT_REMOTE_URL"
  printf 'started_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'runner_host=%s\n' "$(hostname)"
  printf 'bench_location=%s\n' "${BENCH_LOCATION:-unspecified}"
  printf 'bench_route=%s\n' "${BENCH_ROUTE:-unspecified}"
  printf 'cluster_context=%s\n' "${CLUSTER_CONTEXT:-not-applicable}"
  printf 'job_pod_uid=%s\n' "${JOB_POD_UID:-not-applicable}"
  printf 'git_version=%s\n' "$(git --version)"
  printf 'clone_samples=%s\nfetch_samples=%s\n' "$clone_samples" "$fetch_samples"
} >"$evidence_dir/metadata.txt"
printf 'target,operation,sample,exit_code,seconds\n' >"$evidence_dir/timings.csv"

read_uptime() {
  awk '{print $1}' /proc/uptime
}
elapsed() {
  awk -v start="$1" -v end="$2" 'BEGIN { printf "%.2f", end-start }'
}

last_clone=
for ((sample = 1; sample <= clone_samples; sample++)); do
  destination="$scratch/clone-$sample"
  start=$(read_uptime)
  if git clone --quiet --mirror "$GIT_REMOTE_URL" "$destination"; then
    status=0
  else
    status=$?
  fi
  end=$(read_uptime)
  printf '%s,mirror_clone,%s,%s,%s\n' "$target" "$sample" "$status" "$(elapsed "$start" "$end")" >>"$evidence_dir/timings.csv"
  [[ $status == 0 ]] || exit "$status"
  [[ -z $last_clone ]] || rm -rf "$last_clone"
  last_clone=$destination
done

for ((sample = 1; sample <= fetch_samples; sample++)); do
  start=$(read_uptime)
  if git --git-dir="$last_clone" fetch --quiet --prune origin; then
    status=0
  else
    status=$?
  fi
  end=$(read_uptime)
  printf '%s,no_change_fetch,%s,%s,%s\n' "$target" "$sample" "$status" "$(elapsed "$start" "$end")" >>"$evidence_dir/timings.csv"
  [[ $status == 0 ]] || exit "$status"
done

{
  printf 'ref_count=%s\n' "$(git --git-dir="$last_clone" for-each-ref --format='%(refname)' | wc -l | tr -d ' ')"
  printf 'commit_count=%s\n' "$(git --git-dir="$last_clone" rev-list --all --count)"
  printf 'object_count=%s\n' "$(git --git-dir="$last_clone" rev-list --objects --all | wc -l | tr -d ' ')"
  printf 'git_count_objects_begin\n'
  git --git-dir="$last_clone" count-objects -v
  printf 'git_count_objects_end\n'
  printf 'mirror_size_kib=%s\n' "$(du -sk "$last_clone" | awk '{print $1}')"
  printf 'finished_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >>"$evidence_dir/metadata.txt"

printf 'evidence_dir=%s\n' "$evidence_dir"
