#!/usr/bin/env bash
# Run via the real_split_*_test targets registered by sweep.bzl.
set -euo pipefail
prepare=$(rlocation "$1")
lower=$(rlocation "$2")
split=$(rlocation "$3")
summarize=$(rlocation "$4")
bundle=$5
cut_step=$6
entry_first_step=$7
shift 7
if [[ $# -gt 1 || ($# == 1 && $1 != --sample) ]]; then
  echo "usage: pass --test_arg=--sample for a single split" >&2
  exit 2
fi
work="$TEST_TMPDIR/bundles"
results="$TEST_UNDECLARED_OUTPUTS_DIR"
"$prepare" "$work" "$bundle"
"$lower" "$work/$bundle.json" >"$work/footprints.jsonl"
status=0
run() {
  local strategy=$1
  shift
  "$split" "$bundle" "$strategy" "$@" --bundles "$work" --footprints "$work/footprints.jsonl" \
    --out "$results/${bundle}_${strategy}.jsonl" || status=$?
}
run halves
if [[ ${1:-} != --sample ]]; then
  run cuts --step "$cut_step"
  run cuts_entry_first --step "$entry_first_step"
  run blocks
  run random --seeds 20
fi
"$summarize" "$results/"*.jsonl | tee "$results/summary.md"
exit "$status"
