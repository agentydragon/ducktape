#!/usr/bin/env bash
# Run knob ablation tests locally and remotely (via RBE), then collect results.
#
# Usage: bash props/frontend/tests/run_knob_ablation.sh [local|remote|both]
#
# Default: both
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

MODE="${1:-both}"
RESULTS_DIR="props/frontend/tests/knob_ablation_results"
mkdir -p "$RESULTS_DIR"

run_test() {
  local label="$1"
  local extra_args=("${@:2}")

  echo "======================================================================"
  echo "Running knob ablation: $label"
  echo "======================================================================"

  # --test_output=all to stream output
  # --test_env passes the execution environment label
  # --nocache_test_results forces re-execution
  bazel test //props/frontend:knob_ablation \
    --test_output=all \
    --nocache_test_results \
    --test_env="EXECUTION_ENV=$label" \
    "${extra_args[@]}" \
    2>&1 | tee "$RESULTS_DIR/${label}-output.log" || true

  # Copy artifacts from bazel-testlogs
  local testlog_dir
  testlog_dir="$(bazel info bazel-testlogs 2>/dev/null)/props/frontend/knob_ablation/test.outputs"
  if [ -d "$testlog_dir" ]; then
    echo "Copying artifacts from $testlog_dir"
    mkdir -p "$RESULTS_DIR/$label"
    cp -r "$testlog_dir"/* "$RESULTS_DIR/$label/" 2>/dev/null || true
    echo "Artifacts saved to $RESULTS_DIR/$label/"
  else
    echo "WARNING: No test outputs found at $testlog_dir"
  fi
}

case "$MODE" in
  local)
    run_test "local" --spawn_strategy=local
    ;;
  remote)
    run_test "remote" # uses default spawn_strategy (remote,local from buildbuddy.bazelrc)
    ;;
  both)
    run_test "local" --spawn_strategy=local
    run_test "remote"
    ;;
  *)
    echo "Usage: $0 [local|remote|both]"
    exit 1
    ;;
esac

echo ""
echo "======================================================================"
echo "Results in $RESULTS_DIR/"
echo "======================================================================"
ls -la "$RESULTS_DIR/" 2>/dev/null || true
