#!/usr/bin/env bash
# Parallel ansible-lint execution for independent playbooks
#
# Usage:
#   ./scripts/lint-parallel.sh              # Lint all playbooks in parallel
#   ./scripts/lint-parallel.sh atlas.yaml vps.yaml  # Lint specific playbooks
#
# Requires: GNU parallel
# Install: apt-get install parallel || brew install parallel

set -euo pipefail

cd "$(dirname "$0")"

# Performance optimizations
export ANSIBLE_LINT_SKIP_SCHEMA_UPDATE=1

# Determine number of parallel jobs (default: CPU count)
JOBS="${ANSIBLE_LINT_JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"

# If no arguments, find all top-level playbooks
if [ $# -eq 0 ]; then
  playbooks=$(find . -maxdepth 1 -name "*.yaml" -type f ! -name "galaxy.yaml")
else
  playbooks="$@"
fi

# Check if GNU parallel is installed
if ! command -v parallel &>/dev/null; then
  echo "Error: GNU parallel is not installed"
  echo "Install with: apt-get install parallel  (or)  brew install parallel"
  echo ""
  echo "Falling back to sequential execution..."
  echo ""

  for playbook in $playbooks; do
    echo "Linting $playbook..."
    ansible-lint --offline --config-file ../.ansible-lint.yaml "$playbook"
  done
  exit $?
fi

echo "Running ansible-lint on playbooks in parallel (jobs=$JOBS):"
echo "$playbooks" | tr ' ' '\n'
echo ""

# Run ansible-lint in parallel
# --will-cite: Silence citation notice
# -j: Number of parallel jobs
# --halt now,fail=1: Stop all jobs on first failure
# --line-buffer: Print output line by line (better for CI)
# --tagstring: Add playbook name to output lines
echo "$playbooks" | tr ' ' '\n' \
  | parallel --will-cite \
    -j "$JOBS" \
    --halt now,fail=1 \
    --line-buffer \
    --tagstring '[{/.}]' \
    "ansible-lint --offline --config-file ../.ansible-lint.yaml {}"

exit_code=$?

if [ $exit_code -eq 0 ]; then
  echo ""
  echo "✓ All playbooks passed ansible-lint"
else
  echo ""
  echo "✗ Some playbooks failed ansible-lint"
fi

exit $exit_code
