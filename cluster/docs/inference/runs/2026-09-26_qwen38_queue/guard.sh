#!/usr/bin/env bash
# Sourced by queue.sh. Callbacks own resource checks, final-result detection and cleanup.

bounded_ollama_get() {
  # kubectl's request timeout applies separately to discovery retries. Bound the
  # whole command, including DNS/discovery, and kill a client that ignores TERM.
  timeout --kill-after=1s 10s kubectl --request-timeout=10s -n ollama get deployment ollama -o json
}

finish_phase() {
  local status=0
  wait "$phase_pid" || status=$?
  phase_pid=
  return "$status"
}

finish_completed_harbor() {
  # Final result already exists: inference is over, but Harbor/its EXIT cleanup
  # may still be alive. Release GPU resources and allow bounded cleanup time.
  stop_server
  local deadline=$((SECONDS + 30))
  while kill -0 "$phase_pid" 2>/dev/null; do
    if ((SECONDS >= deadline)); then
      note 'Harbor result saved, but cleanup did not exit within 30 seconds'
      printf '%s\n' 'cleanup_timeout_after_result' >"$current_run/termination.txt"
      stop_phase
      return 124
    fi
    sleep 1
  done
  finish_phase
}

run_guarded() {
  setsid "$@" &
  phase_pid=$!
  while kill -0 "$phase_pid" 2>/dev/null; do
    if harbor_finished; then
      finish_completed_harbor
      return $?
    fi
    if ! check_runtime_headroom; then
      # The check can outlive the phase. Preserve its exit status instead of
      # inventing an interruption after completion (the September 26 race).
      if ! kill -0 "$phase_pid" 2>/dev/null; then
        break
      fi
      if harbor_finished; then
        note 'guard check failed after Harbor saved its final result'
        printf '%s\n' 'guard_failed_after_result' >"$current_run/post-result-guard.txt"
        finish_completed_harbor
        return $?
      fi
      note 'resource/service guard stopped an unfinished attempt'
      printf '%s\n' 'resource_or_service_guard' >"$current_run/termination.txt"
      stop_server
      stop_phase
      return 1
    fi
    sleep 15
  done
  finish_phase
}
