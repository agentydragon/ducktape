#!/usr/bin/env bash
# Check that no package pyproject.toml files contain ruff configuration
# All ruff config should be centralized in /ruff.toml
#
# Usage: Called by pre-commit with list of pyproject.toml files to check.
# Pre-commit handles exclusions (venvs, caches, node_modules, etc.)

set -euo pipefail

# Filter out root pyproject.toml and repo-template (intentional exceptions)
# Then check remaining files for [tool.ruff] sections
bad_files=()
for file in "$@"; do
  # Skip intentional exceptions
  case "$file" in
    ./pyproject.toml | pyproject.toml) continue ;;
    *repo-template*) continue ;;
  esac

  # Check if file contains ruff config
  if grep -q "^\[tool\.ruff" "$file" 2>/dev/null; then
    bad_files+=("$file")
  fi
done

if [ ${#bad_files[@]} -gt 0 ]; then
  echo "❌ Found ruff config in package pyproject.toml files (should be in ruff.toml):"
  printf '%s\n' "${bad_files[@]}"
  echo ""
  echo "Ruff configuration must be centralized in /ruff.toml"
  echo "Package pyproject.toml files may only list ruff as a dev dependency"
  exit 1
fi

echo "✓ No ruff configuration in package pyproject.toml files"
