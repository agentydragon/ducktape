#!/usr/bin/env bash
# Source this from the Actions step so its entry shell owns the signal trap.
# A foreground pipeline defers Bash's trap until the command finishes. The wait
# builtin is interruptible, giving bb time to send its remote cancellation RPC.
bb_remote_with_log() {
  local log_path="$1"
  shift
  local log_fd tee_pid bb_pid status cancel_pid
  local cancel_script="$(dirname "${BASH_SOURCE[0]}")/cancel_bb_invocations.py"

  exec {log_fd}> >(tee "$log_path")
  tee_pid=$!
  "$@" >&"$log_fd" 2>&1 &
  bb_pid=$!
  exec {log_fd}>&-

  trap '
    trap "" INT TERM
    echo "bb-remote: forwarding cancellation to bb" >&2
    kill -TERM "$bb_pid" 2>/dev/null || true
    # Stopping the outer runner does not reliably cancel its child RBE actions.
    # IDs are supplied before launch, so this also works with buffered logs.
    python3 "$cancel_script" "${INPUT_BAZEL_INVOCATION_IDS:-}" &
    cancel_pid=$!
    wait "$bb_pid"
    wait "$cancel_pid"
    wait "$tee_pid"
    exit 130
  ' INT TERM
  wait "$bb_pid"
  status=$?
  trap - INT TERM
  # Drain the capture before the caller reads it to produce linkage metadata.
  wait "$tee_pid"
  return "$status"
}

bb_remote_with_log "$@"
