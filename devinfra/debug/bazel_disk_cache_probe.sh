#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: devinfra/debug/bazel_disk_cache_probe.sh [--keep]

Create two temporary git worktrees, build the same synthetic genrule in each,
and verify that the second build gets a Bazel disk-cache hit from the first.

Options:
  --keep   Leave the temporary probe directory in place for inspection.
EOF
}

keep=0
while (($#)); do
  case "$1" in
    --keep)
      keep=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
probe_root="$(mktemp -d /tmp/bazel-disk-cache-probe.XXXXXX)"
wt_a="$probe_root/a"
wt_b="$probe_root/b"
output_a="$probe_root/output-a"
output_b="$probe_root/output-b"
probe_id="$(date +%s%N)-$$"
bash_path="$(command -v bash)"
sleep_path="$(command -v sleep)"
sha256sum_path="$(command -v sha256sum)"

shutdown_bazel() {
  local output_root="$1"
  if [[ -d "$output_root" ]]; then
    bazelisk --output_user_root="$output_root" shutdown >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local status=$?
  shutdown_bazel "$output_a"
  shutdown_bazel "$output_b"
  git -C "$repo_root" worktree remove --force "$wt_a" >/dev/null 2>&1 || true
  git -C "$repo_root" worktree remove --force "$wt_b" >/dev/null 2>&1 || true
  if ((keep)); then
    printf 'Keeping probe directory: %s\n' "$probe_root" >&2
  else
    rm -rf "$probe_root"
  fi
  exit "$status"
}
trap cleanup EXIT

write_probe_package() {
  local wt="$1"
  mkdir -p "$wt/disk_cache_probe"
  cat >"$wt/disk_cache_probe/BUILD.bazel" <<EOF
genrule(
    name = "probe",
    srcs = ["input.txt"],
    outs = ["out.txt"],
    cmd = "$sleep_path 3; $sha256sum_path \$(location input.txt) > \$@",
)
EOF
  printf 'shared probe input %s\n' "$probe_id" >"$wt/disk_cache_probe/input.txt"
}

run_probe_build() {
  local wt="$1"
  local output_root="$2"
  (
    cd "$wt"
    bazelisk --output_user_root="$output_root" build \
      --config=nolint \
      --remote_executor= \
      --remote_cache= \
      --bes_backend= \
      --spawn_strategy=local \
      "--shell_executable=$bash_path" \
      --show_result=0 \
      //disk_cache_probe:probe
  )
}

printf 'Probe root: %s\n' "$probe_root"
printf 'Probe id: %s\n' "$probe_id"

git -C "$repo_root" worktree add --detach "$wt_a" HEAD
git -C "$repo_root" worktree add --detach "$wt_b" HEAD
write_probe_package "$wt_a"
write_probe_package "$wt_b"

printf '\nFirst build: populate disk cache from %s\n' "$wt_a"
run_probe_build "$wt_a" "$output_a" 2>&1 | tee "$probe_root/first.log"

printf '\nSecond build: fresh output base at %s\n' "$wt_b"
run_probe_build "$wt_b" "$output_b" 2>&1 | tee "$probe_root/second.log"

if grep -E 'disk cache hit|remote cache hit' "$probe_root/second.log"; then
  printf '\nPASS: second build reported a cache hit from the shared disk cache.\n'
else
  printf '\nFAIL: second build did not report a disk-cache hit.\n' >&2
  printf 'Inspect logs under: %s\n' "$probe_root" >&2
  keep=1
  exit 1
fi
