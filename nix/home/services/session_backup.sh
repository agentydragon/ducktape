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
snapshot_excludes=()
backup_excludes=()
exclude_directory() {
  local root=$1 harness=$2 relative=$3
  snapshot_excludes+=(--exclude-directory "$root/$relative")
  # Staging is persistent: exclude images created before these rules as well.
  backup_excludes+=(--exclude "$root/$relative" --exclude "$staging_dir/images/$harness/*/$relative")
}

# Hook-generated build state, worktrees, caches, packages and diagnostic logs.
for relative in session-env .tmp cache .cache plugins/cache debug logs; do
  exclude_directory "$claude_home" claude "$relative"
done
for relative in worktrees .tmp tmp packages cache .cache plugins/cache log logs; do
  exclude_directory "$codex_home" codex "$relative"
done
exclude_file() {
  local root=$1 harness=$2 relative=$3
  snapshot_excludes+=(--exclude-file "$root/$relative")
  backup_excludes+=(--exclude "$root/$relative" --exclude "$staging_dir/images/$harness/*/$relative")
}
for relative in stats-cache.json mcp-needs-auth-cache.json plugins/plugin-directory-cache-v2.json daemon.log; do
  exclude_file "$claude_home" claude "$relative"
done
exclude_file "$codex_home" codex models_cache.json
# This SQLite database contains diagnostic logs, not conversation history.
exclude_file "$codex_home" codex logs_2.sqlite
# Codex projects paginated history from the durable rollout JSONL:
# https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/thread-store/src/local/live_writer.rs#L349-L364
exclude_file "$codex_home" codex thread_history_1.sqlite
for database in logs_2.sqlite thread_history_1.sqlite; do
  for suffix in -wal -shm -journal; do
    backup_excludes+=(--exclude "$codex_home/$database$suffix" --exclude "$staging_dir/images/codex/*/$database$suffix")
  done
done

"$python" "$snapshot_script" --output "$staging_dir" \
  --root claude "$claude_home" --root codex "$codex_home" \
  "${snapshot_excludes[@]}" || snapshot_status=$?

if ! "$restic" snapshots --latest 1 >/dev/null 2>&1; then
  "$restic" init
fi

backup_path() {
  # Relative sources avoid recording changing /home and user-directory metadata.
  printf '%s\n' "${1#"$home_dir/"}"
}

"$restic" backup --host "$host" --tag ai-sessions --skip-if-unchanged \
  "${backup_excludes[@]}" -- \
  "$(backup_path "$claude_home")" \
  "$(backup_path "$codex_home")" \
  "$(backup_path "$staging_dir")"

exit "$snapshot_status"
