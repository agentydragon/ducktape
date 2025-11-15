#!/bin/bash
# Test with and without type annotations to isolate the issue

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================"
echo "Testing: Type annotations vs clean code"
echo "======================================================================"
echo

# Backup the files with annotations
cp src/adgn/mcp/_shared/uris.py src/adgn/mcp/_shared/uris.py.with-annotations
cp src/adgn/props/docker_env.py src/adgn/props/docker_env.py.with-annotations

echo "1. Testing WITH type annotations (current state)..."
test-venv/bin/mypy --config-file=pyproject.toml src/adgn/ 2>&1 | tee test-with-annotations.log
echo

echo "2. Removing type annotations..."
# Remove the intermediate variable annotations from uris.py
sed -i 's/    uri: str = \(.*\)/    return \1/' src/adgn/mcp/_shared/uris.py
# Remove from docker_env.py
sed -i 's/        name: str = \(.*\)/        return \1/' src/adgn/props/docker_env.py
sed -i '/^        return name$/d' src/adgn/props/docker_env.py

echo "3. Testing WITHOUT type annotations..."
test-venv/bin/mypy --config-file=pyproject.toml src/adgn/ 2>&1 | tee test-without-annotations.log
echo

echo "======================================================================"
echo "Comparison:"
echo "======================================================================"
if grep -q "no-any-return" test-without-annotations.log; then
    echo "✗ WITHOUT annotations: FAILED with no-any-return errors"
    grep "no-any-return" test-without-annotations.log | wc -l | xargs echo "  Error count:"
else
    echo "✓ WITHOUT annotations: PASSED"
fi

if grep -q "no-any-return" test-with-annotations.log; then
    echo "✗ WITH annotations: FAILED with no-any-return errors"
else
    echo "✓ WITH annotations: PASSED"
fi

echo
echo "Restoring original files with annotations..."
mv src/adgn/mcp/_shared/uris.py.with-annotations src/adgn/mcp/_shared/uris.py
mv src/adgn/props/docker_env.py.with-annotations src/adgn/props/docker_env.py

echo "Done!"
