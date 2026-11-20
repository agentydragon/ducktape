#!/usr/bin/env bash
# JS bundle -> footprints -> Lean kernel check -> ESM files -> run both in Node.
#
#   ./run.sh              build, check, render, compare output
#   ./run.sh --controls   also write each rejected split and show how it fails in Node
#   ./run.sh --fuzz       also run the differential fuzzer (about a minute)
#   ./run.sh --experiments  also build experiments/ (mini-JS semantics, two-tier proofs)
#   ./run.sh --lowering   also lower real bundles to ESTree, compute footprints in Lean, compare with eslint-scope
#   ./run.sh --es-order   also compare the model's module evaluation order with Node on 500 random import graphs
#   ./run.sh --splits     also check 2,740 splits of six real bundles and run each in Node (needs --lowering first; about 3 minutes)
#   ./run.sh --debundle-cases  also run debundle's 207 e2e cases through the checker and write experiments/debundle_cases/results (about 10 seconds)
#   ./run.sh --translation  also run the golden cases of the effect analysis (experiments/effects), then check that tampered output trees are rejected and renamed, reshuffled ones accepted (experiments/translation fixtures)
#   ./run.sh --translation-fuzz  also fuzz the translation checker: every tree it accepts must behave like its bundle in Node (about 20 seconds; FUZZ_COUNT=N sets the bundles, default 150)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v lake >/dev/null; then
  exec nix shell nixpkgs#lean4 nixpkgs#gcc nixpkgs#nodejs nixpkgs#python3 --command "$0" "$@"
fi

step() { printf '\n== %s\n' "$*"; }
want() { [[ " $* " == *" $WANT "* ]]; }

step "1. Front end: example/input/bundle.mjs + example/spec.json -> lean/EsmSplit/Instance.lean"
(cd extract && cp package.json.in package.json && npm ci --no-audit --no-fund --silent)
node extract/extract.mjs example/input/bundle.mjs example/spec.json \
  --variant example/variants/cycle.json \
  --control example/controls/claimed_twice.json --control example/controls/cycle_tdz.json \
  --control example/controls/effect_order.json >lean/EsmSplit/Instance.lean

cd lean
step "2. Lean kernel checks every theorem (example, variant, controls)"
lake build

step "3. Axioms behind the theorems, and an independent kernel replay"
lake env lean EsmSplit/Audit.lean
lake env leanchecker EsmSplit && echo "leanchecker: ok"

step "4. Render the verified split into example/output/"
rm -rf ../example/output
lake exe render ../example/output

step "5. Run bundle and split in Node"
cd ..
diff <(node example/input/bundle.mjs) <(cd example/output && node main.mjs)
echo "same stdout:"
node example/input/bundle.mjs

# What `node main.mjs` does in directory $1: its exact stdout, or the first error line.
show() {
  (cd "$1" && node -e '
    const p = require("child_process").spawnSync("node", ["main.mjs"], { encoding: "utf8" });
    console.log("  node:", (p.stderr.match(/^\w*Error: .*/m) || [])[0] ?? JSON.stringify(p.stdout));')
}

if [[ " $* " == *" --controls "* ]]; then
  step "Controls: the checks reject these; here is what Node does with them anyway"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  echo "the bundle prints:"
  (cd example/input && node -e '
    console.log("  node:", JSON.stringify(require("child_process").spawnSync("node", ["bundle.mjs"], { encoding: "utf8" }).stdout))')
  for c in effect_order cycle_tdz claimed_twice missing_import dropped_module; do
    echo "--- $c"
    (cd lean && lake exe render "$tmp/$c" --force "$c" | grep FAIL | sed 's/^ */  /') || true
    show "$tmp/$c"
  done
fi

if [[ " $* " == *" --fuzz "* ]]; then
  step "Fuzz: random splits, checker verdict vs. what Node does"
  python3 fuzz/fuzz.py 600 1
fi

if [[ " $* " == *" --experiments "* ]]; then
  step "Experiments: mini-JS semantics and two-tier proofs"
  (cd lean && lake build Experiments)
fi

if [[ " $* " == *" --lowering "* ]]; then
  step "Lowering: real bundles -> ESTree JSON -> footprints in Lean, checked against eslint-scope"
  (cd experiments/lowering && cp package.json.in package.json && npm ci --no-audit --no-fund --silent && node bundle.mjs \
    && node dump.mjs ../../example/input/bundle.mjs >example.json && node dump.mjs torture.mjs >torture.json \
    && for f in lodash acorn semver zod acorn.min zod.min; do node dump.mjs bundles/$f.mjs >bundles/$f.json; done)
  (cd lean && lake build lower lowersplit)
  (cd experiments/lowering && node compare.mjs example.json --example && node compare.mjs torture.json bundles/*.json)
  step "Lowering: one Lean program, JSON AST + spec -> modules + kernel certificate (no extract.mjs)"
  out=$(mktemp -d)
  lean/.lake/build/bin/lowersplit experiments/lowering/example.json example/spec.json "$out" --lean lean/Experiments/Generated.lean
  rm -rf "$out"
  (cd lean && lake env lean Experiments/Generated.lean)
fi

if [[ " $* " == *" --es-order "* ]]; then
  step "Module order: the model against Node, on random import graphs"
  (cd lean && lake build evalorder)
  node experiments/es_order/check.mjs 500 1
fi

if [[ " $* " == *" --splits "* ]]; then
  step "Splits: halves, cuts, blocks and random placements of real bundles, checked and run in Node"
  (cd lean && lake build lowersplit)
  experiments/splits/run_all.sh
fi

if [[ " $* " == *" --debundle-cases "* ]]; then
  step "debundle's e2e cases: each spec translated, checked by Lean, the emitted modules run in Node"
  (cd lean && lake build lowersplit)
  (cd experiments/debundle_cases && cp package.json.in package.json && npm ci --no-audit --no-fund --silent \
    && node run_cases.mjs && node summarize.mjs results/cases.jsonl >results/summary.md)
fi

if [[ " $* " == *" --translation-fuzz "* ]]; then
  step "Translation fuzzer: random bundles split at random; a tree the checker accepts must behave like its bundle in Node"
  (cd lean && lake build translate)
  (cd experiments/translation && cp package.json.in package.json && npm ci --no-audit --no-fund --silent \
    && node fuzz.mjs --count "${FUZZ_COUNT:-150}" --seed 1)
fi

if [[ " $* " == *" --translation "* ]]; then
  step "Effect analysis: golden cases; translation validation: an output tree is accepted only if it is the bundle, renamed and reshuffled"
  (cd lean && lake build translate lower)
  (cd experiments/translation && cp package.json.in package.json && npm ci --no-audit --no-fund --silent \
    && node ../effects/run.mjs && node test.mjs)
fi
