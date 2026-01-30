#!/usr/bin/env bash
# Fast syntax check for Ansible files in pre-commit
# Only runs ansible-playbook --syntax-check on playbooks (much faster than ansible-lint)
# Full ansible-lint validation happens in CI

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Change to ansible directory (parent of scripts)
cd "$SCRIPT_DIR/.."

# Export SKIP_VAULT to avoid password prompts
export ANSIBLE_LINT_SKIP_VAULT=1

# Root-level YAML files that aren't playbooks (inventory, requirements, etc.)
EXCLUDED_FILES=("galaxy.yaml" "requirements.yaml" "inventory.yaml")

# Helper: check if a filename is excluded
is_excluded() {
  local file=$1
  for excluded in "${EXCLUDED_FILES[@]}"; do
    if [[ "$file" == "$excluded" ]]; then
      return 0
    fi
  done
  return 1
}

# Strip "ansible/" prefix from file paths if present
# Pre-commit passes paths like "ansible/roles/cli/tasks/main.yml"
# but we need "roles/cli/tasks/main.yml"
files=()
for arg in "$@"; do
  stripped="${arg#ansible/}"
  files+=("$stripped")
done

# Determine which playbooks to check
playbooks=()
other_files=()

if [ ${#files[@]} -eq 0 ]; then
  # No arguments: find all playbooks in current directory
  find_cmd=(find . -maxdepth 1 -name "*.yaml" -type f)
  for excluded in "${EXCLUDED_FILES[@]}"; do
    find_cmd+=(! -name "$excluded")
  done
  mapfile -t playbooks < <("${find_cmd[@]}")
else
  # Arguments provided: filter to playbooks only
  for file in "${files[@]}"; do
    # Check if it's a playbook (*.yaml at root level, not in subdirs)
    if [[ "$file" =~ ^[^/]+\.yaml$ ]] && ! is_excluded "$file"; then
      playbooks+=("$file")
    else
      # Role files, vars, etc. - skip syntax check (prettier will catch YAML issues)
      other_files+=("$file")
    fi
  done
fi

# Exit early if no playbooks to check
if [ ${#playbooks[@]} -eq 0 ]; then
  if [ ${#other_files[@]} -gt 0 ]; then
    echo "Note: Non-playbook files (${#other_files[@]} files) validated by prettier hook"
  else
    echo "No playbooks found to syntax check"
  fi
  exit 0
fi

# Syntax check all playbooks
exit_code=0
for playbook in "${playbooks[@]}"; do
  echo "Syntax checking: $playbook"
  if ! ansible-playbook --syntax-check "$playbook"; then
    exit_code=1
  fi
done

# Note about non-playbook files
if [ ${#other_files[@]} -gt 0 ]; then
  echo "Note: Non-playbook files (${#other_files[@]} files) validated by prettier hook"
fi

exit $exit_code
