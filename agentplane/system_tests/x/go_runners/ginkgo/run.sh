#!/usr/bin/env bash
set -euo pipefail

# Ginkgo's coordinator starts three workers from the binary Bazel already compiled.
# Only the coordinator writes JUnit; rules_go's per-process XML wrapper would race it.
report="$XML_OUTPUT_FILE"
unset XML_OUTPUT_FILE
export GO_TEST_WRAP=0
args=(--procs=3 --seed=1 --no-color --fail-on-empty --output-dir="$(dirname "$report")" --junit-report="$(basename "$report")")
if [[ -n "${TESTBRIDGE_TEST_ONLY:-}" ]]; then
  args+=(--focus="$TESTBRIDGE_TEST_ONLY")
fi
# rules_go otherwise treats Bazel's spec filter as a filter for TestSharedStack itself.
unset TESTBRIDGE_TEST_ONLY
exec "$TEST_SRCDIR/$GINKGO_CLI" "${args[@]}" "$TEST_SRCDIR/$GINKGO_SUITE" "$@"
