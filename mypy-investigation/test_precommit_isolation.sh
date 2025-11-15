#!/bin/bash
# Test if pre-commit isolation affects type checking
# Usage: ./test_precommit_isolation.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$SCRIPT_DIR/test_case"
RESULTS_DIR="$SCRIPT_DIR/results"

mkdir -p "$RESULTS_DIR"

echo "==================================================================="
echo "Testing pre-commit isolation effect on type checking"
echo "==================================================================="
echo

# Create a minimal pre-commit config for testing
cat > "$SCRIPT_DIR/.pre-commit-config.yaml" << 'EOF'
repos:
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.18.2
    hooks:
      - id: mypy
        name: mypy-default
        args: ["--python-version=3.12", "--warn-return-any"]
        files: "test_case/.*\\.py$"
      - id: mypy
        name: mypy-no-cache
        args: ["--python-version=3.12", "--warn-return-any", "--disable-expression-cache"]
        files: "test_case/.*\\.py$"
  - repo: local
    hooks:
      - id: pyright-local
        name: pyright
        entry: pyright
        language: system
        types: [python]
        files: "test_case/.*\\.py$"
        pass_filenames: true
EOF

# Initialize git repo if not already done
if [ ! -d "$SCRIPT_DIR/.git" ]; then
    cd "$SCRIPT_DIR"
    git init
    git add .
fi

# Run pre-commit
echo "Running pre-commit with mypy (default config)..."
cd "$SCRIPT_DIR"
pre-commit run mypy-default --all-files 2>&1 | tee "$RESULTS_DIR/precommit-mypy-default.log" || true

echo
echo "Running pre-commit with mypy (no cache)..."
pre-commit run mypy-no-cache --all-files 2>&1 | tee "$RESULTS_DIR/precommit-mypy-no-cache.log" || true

echo
echo "Running pre-commit with pyright..."
if command -v pyright &> /dev/null; then
    pre-commit run pyright-local --all-files 2>&1 | tee "$RESULTS_DIR/precommit-pyright.log" || true
else
    echo "⚠ Pyright not installed, skipping"
fi

echo
echo "==================================================================="
echo "Pre-commit isolation test complete"
echo "==================================================================="
