#!/usr/bin/env bash
# Rerunner: fact-based selector resolver profile (post-#2398), source_match
# selectors over the public synthetic corpus. Ir-only callgrind, no cache-sim.
# Build the -c opt debundle binary with -Cdebuginfo=1 first (symbolized stacks).
set -euo pipefail
BIN=bazel-out/k8-opt/bin/devinfra/js/debundle/debundle
# 1. Public corpus (gaffer-scale shape; see perf/proposer.md + gen_synth_corpus.py):
python3 devinfra/js/debundle/perf/gen_synth_corpus.py \
  --out /tmp/synth62 --statements 10000 --seed 1 --claim-blocks 62
# 2. Rewrite member selectors binding-name -> source_match so the production
#    fact-based ChunkResolver (chunk_facts EDB + selector_match homomorphism)
#    is exercised. (/tmp/make_source_match_spec.py — see perf note.)
python3 /tmp/make_source_match_spec.py /tmp/synth62
# 3. Callgrind, Ir-only:
valgrind --tool=callgrind --callgrind-out-file=callgrind.source_match_run.out \
  --cache-sim=no --branch-sim=no --dump-instr=no --collect-jumps=no \
  "$BIN" run --spec /tmp/synth62/spec_sourcematch.json >/dev/null
