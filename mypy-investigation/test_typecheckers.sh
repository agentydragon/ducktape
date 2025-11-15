#!/bin/bash
# Test script to investigate Final[str] type inference across different type checkers
# Usage: ./test_typecheckers.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$SCRIPT_DIR/test_case"
RESULTS_DIR="$SCRIPT_DIR/results"

mkdir -p "$RESULTS_DIR"

echo "==================================================================="
echo "Testing Final[str] type inference with various type checkers"
echo "==================================================================="
echo

# Test with system pyright if available
echo "--- Testing with pyright ---"
if command -v pyright &> /dev/null; then
    pyright --pythonversion 3.12 "$TEST_DIR" 2>&1 | tee "$RESULTS_DIR/pyright.log"
    echo "✓ Pyright test complete"
else
    echo "⚠ Pyright not installed, skipping"
fi
echo

# Test with various mypy versions
MYPY_VERSIONS=("1.14.0" "1.15.0" "1.16.0" "1.17.0" "1.18.1" "1.18.2")

for version in "${MYPY_VERSIONS[@]}"; do
    echo "--- Testing with mypy $version ---"

    # Create virtualenv for this version
    VENV_DIR="$RESULTS_DIR/venv-mypy-$version"
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet "mypy==$version"
    fi

    # Test with default config
    echo "  Config: warn_return_any=true, no cache flags"
    cat > "$TEST_DIR/mypy.ini" << EOF
[mypy]
python_version = 3.12
warn_return_any = true
EOF
    "$VENV_DIR/bin/mypy" --config-file="$TEST_DIR/mypy.ini" "$TEST_DIR" 2>&1 | tee "$RESULTS_DIR/mypy-$version-default.log"

    # Test with disable_expression_cache
    echo "  Config: warn_return_any=true, disable_expression_cache=true"
    cat > "$TEST_DIR/mypy.ini" << EOF
[mypy]
python_version = 3.12
warn_return_any = true
disable_expression_cache = true
EOF
    "$VENV_DIR/bin/mypy" --config-file="$TEST_DIR/mypy.ini" "$TEST_DIR" 2>&1 | tee "$RESULTS_DIR/mypy-$version-no-cache.log"

    echo
done

# Clean up mypy.ini
rm -f "$TEST_DIR/mypy.ini"

echo "==================================================================="
echo "Test complete. Results saved to: $RESULTS_DIR"
echo "==================================================================="
echo
echo "Summary:"
for log in "$RESULTS_DIR"/*.log; do
    basename=$(basename "$log" .log)
    if grep -q "Success: no issues found" "$log" 2>/dev/null; then
        echo "✓ $basename: PASSED"
    elif grep -q "no-any-return" "$log" 2>/dev/null; then
        count=$(grep -c "no-any-return" "$log" || true)
        echo "✗ $basename: FAILED ($count no-any-return errors)"
    else
        echo "? $basename: UNKNOWN STATUS"
    fi
done
