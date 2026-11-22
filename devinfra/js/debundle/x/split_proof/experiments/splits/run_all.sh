#!/usr/bin/env bash
# Run via //devinfra/js/debundle/x/split_proof:real_split_sweep.
set -euo pipefail
prepare=$(rlocation "$1")
lower=$(rlocation "$2")
split=$(rlocation "$3")
summarize=$(rlocation "$4")
shift 4
if [[ ${1:-} != /* || $# -gt 2 || ($# == 2 && $2 != --sample) ]]; then
  echo "usage: real_split_sweep <absolute results dir> [--sample]" >&2
  exit 2
fi
mkdir -p "$1"
results=$(mktemp -d "$1/real_splits.XXXXXX")
work=$(mktemp -d "${TMPDIR:-/tmp}/split_bundles.XXXXXX")
trap 'rm -rf "$work"' EXIT
"$prepare" "$work"
status=0
analyze() { "$lower" "$work/$1.json" >"$work/$1.jsonl"; }
run() {
  local bundle=$1 strategy=$2
  shift 2
  "$split" "$bundle" "$strategy" "$@" --bundles "$work" --footprints "$work/$bundle.jsonl" \
    --out "$results/${bundle}_${strategy}.jsonl" || status=$?
}
if [[ ${2:-} == --sample ]]; then
  analyze semver
  run semver halves
else
  for b in semver lodash acorn acorn.min; do
    analyze "$b"
    run "$b" halves
    run "$b" cuts
    run "$b" cuts_entry_first --step 10
    run "$b" blocks
    run "$b" random --seeds 20
  done
  for b in zod zod.min; do
    analyze "$b"
    run "$b" halves
    run "$b" cuts --step 4
    run "$b" cuts_entry_first --step 40
    run "$b" blocks
    run "$b" random --seeds 20
  done
fi
"$summarize" "$results/"*.jsonl | tee "$results/summary.md"
echo "Results: $results"
exit "$status"
