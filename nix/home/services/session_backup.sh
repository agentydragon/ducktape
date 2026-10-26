#!/usr/bin/env bash
set -euo pipefail

restic=$1
python=$2
snapshot_script=$3
home_dir=$4
claude_home=$5
codex_home=$6
staging_dir=$7
host=$8

cd "$home_dir"
export TMPDIR="${RUNTIME_DIRECTORY:?}"

# Preserve raw files even when a damaged database cannot produce a consistent
# image. The persisted manifest records errors, and the unit still fails.
snapshot_status=0
# Hook-generated runtime/build state; see home_session_dir and write_session_bazelrc
# in devinfra/claude/claude_hook/main.rs. Keep this out of both capture paths.
claude_session_env="$claude_home/session-env"
codex_worktrees="$codex_home/worktrees"
"$python" "$snapshot_script" --output "$staging_dir" \
  --root claude "$claude_home" --root codex "$codex_home" \
  --exclude-directory "$claude_session_env" \
  --exclude-directory "$codex_worktrees" || snapshot_status=$?

if ! "$restic" snapshots --latest 1 >/dev/null 2>&1; then
  "$restic" init
fi

backup_path() {
  # Relative sources avoid recording changing /home and user-directory metadata.
  printf '%s\n' "${1#"$home_dir/"}"
}

"$restic" backup --host "$host" --tag ai-sessions --skip-if-unchanged \
  --exclude "$claude_session_env" \
  --exclude "$codex_worktrees" \
  --exclude "$staging_dir/images/claude/*/session-env" \
  --exclude "$staging_dir/images/codex/*/worktrees" -- \
  "$(backup_path "$claude_home")" \
  "$(backup_path "$codex_home")" \
  "$(backup_path "$staging_dir")"

exit "$snapshot_status"
