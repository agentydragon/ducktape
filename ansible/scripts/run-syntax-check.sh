#!/usr/bin/env bash
# Fast syntax check for Ansible files in pre-commit
# Only runs ansible-playbook --syntax-check on playbooks (much faster than ansible-lint)
# Full ansible-lint validation happens in CI
#
# Pre-commit's files/exclude patterns ensure only root-level playbook YAML
# files are passed here. Non-playbooks and subdirectory files are filtered out
# before this script runs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Change to ansible directory (parent of scripts)
cd "$SCRIPT_DIR/.."

# Export SKIP_VAULT to avoid password prompts
export ANSIBLE_LINT_SKIP_VAULT=1

# Syntax check each playbook passed by pre-commit.
# Pre-commit passes paths like "ansible/agentydragon.yaml" — strip prefix.
exit_code=0
for arg in "$@"; do
  playbook="${arg#ansible/}"
  echo "Syntax checking: $playbook"
  if ! ansible-playbook --syntax-check "$playbook"; then
    exit_code=1
  fi
done

exit $exit_code
