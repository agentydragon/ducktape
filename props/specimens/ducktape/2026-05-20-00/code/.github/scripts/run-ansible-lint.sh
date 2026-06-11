#!/usr/bin/env bash
# Run full ansible-lint on all playbooks
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
ANSIBLE_DIR="${REPO_ROOT}/ansible"

# Full thorough check - no NODEPS, no --offline
# This validates module parameters, dependencies, etc.
# List all playbooks to lint
playbooks=$(find "$ANSIBLE_DIR" -maxdepth 1 -name "*.yaml" -type f ! -name "galaxy.yaml")

echo "Running full ansible-lint on playbooks:"
echo "$playbooks"
echo ""

# Run on all playbooks (--project-dir sets cwd for ansible-lint resolution)
ansible-lint --project-dir "$ANSIBLE_DIR" --config-file "${REPO_ROOT}/.ansible-lint.yaml" $playbooks
