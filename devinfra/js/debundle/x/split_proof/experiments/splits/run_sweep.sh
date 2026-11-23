#!/usr/bin/env bash
# Run via the real_split_*_test targets registered by sweep.bzl.
set -euo pipefail
lower=$(rlocation "$1")
split=$(rlocation "$2")
summarize=$(rlocation "$3")
source=$(rlocation "$4")
ast=$(rlocation "$5")
bundle=$6
cut_step=$7
entry_first_step=$8
shift 8
if [[ $# -gt 1 || ($# == 1 && $1 != --sample) ]]; then
  echo "usage: pass --test_arg=--sample for a single split" >&2
  exit 2
fi
[[ -f $source && -f $ast ]]
bundles=$(dirname "$source")
footprints="$TEST_TMPDIR/footprints.jsonl"
results="$TEST_UNDECLARED_OUTPUTS_DIR"
"$lower" "$ast" >"$footprints"
status=0
run() {
  local strategy=$1
  shift
  "$split" "$bundle" "$strategy" "$@" --bundles "$bundles" --footprints "$footprints" \
    --out "$results/${bundle}_${strategy}.jsonl" || status=$?
}
run halves --require-accepted
if [[ ${1:-} != --sample ]]; then
  run cuts --step "$cut_step"
  run cuts_entry_first --step "$entry_first_step"
  run blocks
  run random --seeds 20
fi
"$summarize" "$results/"*.jsonl | tee "$results/summary.md"
exit "$status"
