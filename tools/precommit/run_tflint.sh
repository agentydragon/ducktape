#!/usr/bin/env bash
# Pre-commit hook for tflint: initializes plugins then lints each changed
# Terraform module directory.
# tflint binary is installed by pre-commit via language: golang.
set -euo pipefail

# Skip in gVisor (Claude Code web) - tflint plugins fail with "bad address"
# due to gVisor sandbox limitations with certain system calls.
if [[ "${CLAUDE_CODE_REMOTE:-false}" == "true" ]]; then
  echo "tflint: skipped (gVisor environment - plugin incompatibility)" >&2
  exit 0
fi

REPO_ROOT="$(pwd)"
CONFIG="${REPO_ROOT}/cluster/.tflint.hcl"

# Initialize plugins (downloads to ~/.tflint.d/plugins/)
tflint --init --config="$CONFIG" >&2

# Group file args by directory and lint each unique directory
declare -A dirs
for f in "$@"; do
  dirs["$(dirname "$f")"]=1
done

exit_code=0
for dir in "${!dirs[@]}"; do
  if ! tflint --chdir="$dir" --config="$CONFIG"; then
    exit_code=1
  fi
done

exit "$exit_code"
