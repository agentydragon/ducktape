#!/usr/bin/env bash
# Every split strategy on every real bundle; one JSON-lines file per (bundle, strategy) in results/.
# Needs `lean/.lake/build/bin/{lower,lowersplit}` and the bundles from `./run.sh --lowering`. About 10 minutes,
# most of it the footprint analysis of zod, which runs once per bundle.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p results
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
status=0
analyze() { ../../lean/.lake/build/bin/lower "../lowering/bundles/$1.json" >"$work/$1.jsonl" 2>/dev/null; }
run() { node run_splits.mjs "$1" "${@:2}" --footprints "$work/$1.jsonl" || status=$?; }
for b in semver lodash acorn acorn.min; do
  analyze "$b"
  run "$b" halves --out "results/${b}_halves.jsonl"
  run "$b" cuts --out "results/${b}_cuts.jsonl"
  run "$b" cuts_entry_first --step 10 --out "results/${b}_cuts_entry_first.jsonl"
  run "$b" blocks --out "results/${b}_blocks.jsonl"
  run "$b" random --seeds 20 --out "results/${b}_random.jsonl"
done
for b in zod zod.min; do
  analyze "$b"
  run "$b" halves --out "results/${b}_halves.jsonl"
  run "$b" cuts --step 4 --out "results/${b}_cuts.jsonl"
  run "$b" cuts_entry_first --step 40 --out "results/${b}_cuts_entry_first.jsonl"
  run "$b" blocks --out "results/${b}_blocks.jsonl"
  run "$b" random --seeds 20 --out "results/${b}_random.jsonl"
done
node summarize.mjs results/*.jsonl | tee results/summary.md
exit "$status"
