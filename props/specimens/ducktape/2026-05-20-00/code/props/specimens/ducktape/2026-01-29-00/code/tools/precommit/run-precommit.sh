#!/bin/bash
# Run the precommit script without holding Bazel lock during execution.
#
# Uses PPID to identify the pre-commit invocation - all batches from one
# pre-commit run share the same runner script. First batch generates it,
# others wait and reuse it.
#
# Stores runners in .git/precommit-runners/ (or worktree's git dir).

set -e

GIT_DIR="$(git rev-parse --git-dir)"
CACHE_DIR="$GIT_DIR/precommit-runners"
RUNNER_SCRIPT="$CACHE_DIR/runner-$PPID.sh"
LOCK_FILE="$CACHE_DIR/lock-$PPID"

mkdir -p "$CACHE_DIR"

# Use flock to ensure only one batch generates the script
# Close fd 200 before bazelisk to prevent bazel server from inheriting the lock
(
  flock -x 200
  if [[ ! -x "$RUNNER_SCRIPT" ]]; then
    bazelisk run --script_path="$RUNNER_SCRIPT" //tools/precommit >/dev/null 200>&-
  fi
) 200>"$LOCK_FILE"

# Execute without bazel lock
exec "$RUNNER_SCRIPT" "$@"
