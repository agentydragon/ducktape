#!/usr/bin/env bash
# Exercise process races with real children; no Docker, Kubernetes or GPUs.
set -euo pipefail
source "$1"
current_run="$TEST_TMPDIR/run"
mkdir -p "$current_run"
export current_run
phase_pid=
trap '[[ -z $phase_pid ]] || kill -KILL -- "-$phase_pid" 2>/dev/null || true' EXIT
note() { echo "$*"; }
stop_server() { touch "$current_run/server-stopped"; }
stop_phase() {
  kill -TERM -- "-$phase_pid" 2>/dev/null || true
  wait "$phase_pid" || true
  phase_pid=
}
harbor_finished() { [[ -f $current_run/result-final ]]; }
wait_file() {
  local deadline=$((SECONDS + 5))
  until [[ -e $1 ]]; do
    ((SECONDS < deadline)) || return 1
    sleep 0.01
  done
}
reset_case() { rm -f "$current_run"/*; }

# A failed slow check must not overwrite a child that finished in the meantime;
# preserve both success and a nonzero exit status.
for expected in 0 7; do
  reset_case
  check_runtime_headroom() {
    wait_file "$current_run/ready"
    touch "$current_run/release"
    while kill -0 "$phase_pid" 2>/dev/null; do sleep 0.01; done
    return 1
  }
  status=0
  run_guarded bash -c 'touch "$current_run/ready"; until [[ -f $current_run/release ]]; do sleep 0.01; done; exit "$1"' _ "$expected" || status=$?
  [[ $status == "$expected" && ! -e $current_run/termination.txt && ! -e $current_run/server-stopped ]]
done

# A completed Harbor result may precede process exit. Release the server and
# permit cleanup, without misclassifying the result as an interrupted attempt.
reset_case
check_runtime_headroom() {
  wait_file "$current_run/ready"
  touch "$current_run/release"
  wait_file "$current_run/result-final"
  return 1
}
run_guarded bash -c 'touch "$current_run/ready"; until [[ -f $current_run/release ]]; do sleep 0.01; done; touch "$current_run/result-final"; until [[ -f $current_run/server-stopped ]]; do sleep 0.01; done'
[[ ! -e $current_run/termination.txt && -e $current_run/post-result-guard.txt ]]

# An unfinished task must still be stopped on a failed resource/service check.
reset_case
check_runtime_headroom() {
  wait_file "$current_run/ready"
  return 1
}
status=0
run_guarded bash -c 'touch "$current_run/ready"; exec sleep 300' || status=$?
[[ $status == 1 && -e $current_run/server-stopped ]]
[[ $(cat "$current_run/termination.txt") == resource_or_service_guard ]]

# A result does not excuse an indefinitely stuck cleanup process.
reset_case
check_runtime_headroom() {
  wait_file "$current_run/result-final"
  return 1
}
status=0
run_guarded bash -c 'touch "$current_run/result-final"; exec sleep 300' || status=$?
[[ $status == 124 && -e $current_run/server-stopped ]]
[[ $(cat "$current_run/termination.txt") == cleanup_timeout_after_result ]]

# Simulate a kubectl client stuck in discovery that ignores SIGTERM. The actual
# production timeout must bound the entire invocation, not individual requests.
mkdir -p "$TEST_TMPDIR/bin"
cat >"$TEST_TMPDIR/bin/kubectl" <<'EOF'
#!/bin/bash
trap '' TERM
while :; do sleep 1; done
EOF
chmod +x "$TEST_TMPDIR/bin/kubectl"
export PATH="$TEST_TMPDIR/bin:$PATH"
start=$SECONDS
status=0
bounded_ollama_get || status=$?
[[ $status != 0 ]]
((SECONDS - start < 20))
echo 'Guard process regressions passed'
