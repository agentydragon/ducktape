#!/usr/bin/env bash
set -euo pipefail

# Run from a checkout containing this experiment. The remote build stages only
# these files into a separate Bazel workspace, avoiding the root module graph.
bbr --script '
  set -eu
  task_day=$(date -u +%Y_%m_%d)
  mkdir -p "/tmp/agents/$task_day/js-module-proof"
  task_work=$(mktemp -d "/tmp/agents/$task_day/js-module-proof/bazel.XXXXXX")
  cp -a x/js_module_proof/standalone/. "$task_work/"
  cp -a x/js_module_proof/Semantics.lean x/js_module_proof/Counter.lean x/js_module_proof/checker.mjs x/js_module_proof/Effects.lean x/js_module_proof/effects.mjs x/js_module_proof/Combined.lean x/js_module_proof/combined.mjs x/js_module_proof/Bindings.lean x/js_module_proof/bindings.mjs \
    x/js_module_proof/main.mjs x/js_module_proof/end_to_end_test.mjs x/js_module_proof/corpus_test.mjs \
    x/js_module_proof/bindings_test.mjs x/js_module_proof/initialization_order_test.mjs x/js_module_proof/effect_graph_corpus_test.mjs x/js_module_proof/combined_behavior_test.mjs \
    x/js_module_proof/example "$task_work/"
  cd "$task_work"
  bazelisk test //:tests --test_output=errors
'
