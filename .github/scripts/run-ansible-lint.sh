#!/usr/bin/env bash
# Run full ansible-lint on all playbooks
set -euo pipefail

cd "$(dirname "$0")/../../ansible"

# Full thorough check - no NODEPS, no --offline
# This validates module parameters, dependencies, etc.
export ANSIBLE_LINT_SKIP_VAULT=1

# List all playbooks to lint
playbooks=$(find . -maxdepth 1 -name "*.yaml" -type f ! -name "galaxy.yaml")

echo "Running full ansible-lint on playbooks:"
echo "$playbooks"
echo ""

# Run on all playbooks
# Intentional word splitting: $playbooks contains multiple filenames
# shellcheck disable=SC2086
ansible-lint --config-file ../.ansible-lint.yaml $playbooks
