#!/bin/bash
# validate-dag-split.sh - Validate branch split against all DAG orderings
#
# Usage: ./validate-dag-split.sh dag.json [--skip-tests]
#
# Validates:
# 1. All branches merge cleanly in every valid DAG ordering
# 2. No new test failures introduced (compared to baseline)
# 3. Union of all PR diffs equals original branch diff (content invariant)
#
# Requires: jq, git, python3

set -euo pipefail

DAG_FILE="${1:?Usage: $0 dag.json [--skip-tests]}"
SKIP_TESTS="${2:-}"
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Working directory: $WORK_DIR"

# Parse DAG
BASE=$(jq -r '.base' "$DAG_FILE")
ORIGINAL_BRANCH=$(jq -r '.original_branch' "$DAG_FILE")
TEST_CMD=$(jq -r '.test_command // "true"' "$DAG_FILE")
BUILD_CMD=$(jq -r '.build_command // "true"' "$DAG_FILE")

echo "=== Configuration ==="
echo "Base branch: $BASE"
echo "Original branch: $ORIGINAL_BRANCH"
echo "Test command: $TEST_CMD"
echo "Build command: $BUILD_CMD"
echo ""

# Get original branch diff (this is what the split must sum to)
echo "=== Capturing original branch diff ==="
git diff "$BASE...$ORIGINAL_BRANCH" >"$WORK_DIR/original.diff"
ORIGINAL_DIFF_LINES=$(wc -l <"$WORK_DIR/original.diff")
echo "Original diff: $ORIGINAL_DIFF_LINES lines"

# Establish baseline (what already fails on base)
echo ""
echo "=== Establishing baseline on $BASE ==="
git worktree add "$WORK_DIR/baseline" "$BASE" --detach 2>/dev/null
pushd "$WORK_DIR/baseline" >/dev/null
if [ "$SKIP_TESTS" != "--skip-tests" ]; then
  BASELINE_FAILURES=$($TEST_CMD 2>&1 | grep -E "FAILED|ERROR" || true)
  echo "Baseline failures: $(echo "$BASELINE_FAILURES" | grep -c . || echo 0) targets"
else
  BASELINE_FAILURES=""
  echo "Skipping baseline tests (--skip-tests)"
fi
popd >/dev/null

# Generate all topological orderings
echo ""
echo "=== Generating valid DAG orderings ==="
python3 - "$DAG_FILE" >"$WORK_DIR/orderings.txt" <<'PYTHON'
import json
import sys
from itertools import permutations

with open(sys.argv[1]) as f:
    dag = json.load(f)["branches"]

def is_valid_ordering(order, dag):
    seen = set()
    for node in order:
        for dep in dag.get(node, []):
            if dep not in seen:
                return False
        seen.add(node)
    return True

nodes = list(dag.keys())
valid = [o for o in permutations(nodes) if is_valid_ordering(o, dag)]
for o in valid:
    print(" ".join(o))

print(f"Generated {len(valid)} valid orderings", file=sys.stderr)
PYTHON

TOTAL=$(wc -l <"$WORK_DIR/orderings.txt")
echo "Valid orderings: $TOTAL"

if [ "$TOTAL" -eq 0 ]; then
  echo "ERROR: No valid orderings found. Check DAG for cycles or missing branches."
  exit 1
fi

# Test each ordering
echo ""
echo "=== Testing $TOTAL valid DAG orderings ==="

ORDERING_NUM=0
while read -r ORDERING; do
  ORDERING_NUM=$((ORDERING_NUM + 1))
  echo "--- Ordering $ORDERING_NUM/$TOTAL: $ORDERING ---"

  # Create fresh worktree from base
  WORKTREE="$WORK_DIR/test-$ORDERING_NUM"
  git worktree add "$WORKTREE" "$BASE" --detach 2>/dev/null

  pushd "$WORKTREE" >/dev/null

  # Disable signing in this worktree
  git config commit.gpgsign false

  for BRANCH in $ORDERING; do
    echo "  Merging $BRANCH..."
    if ! git merge --no-edit "origin/$BRANCH" 2>&1; then
      echo "FAIL: Conflict merging $BRANCH in ordering $ORDERING_NUM"
      popd >/dev/null
      exit 1
    fi
  done

  # Run tests if not skipped
  if [ "$SKIP_TESTS" != "--skip-tests" ]; then
    echo "  Running tests..."
    CURRENT_FAILURES=$($TEST_CMD 2>&1 | grep -E "FAILED|ERROR" || true)
    NEW_FAILURES=$(comm -23 <(echo "$CURRENT_FAILURES" | sort -u) <(echo "$BASELINE_FAILURES" | sort -u) || true)

    if [ -n "$NEW_FAILURES" ]; then
      echo "FAIL: New test failures in ordering $ORDERING_NUM:"
      echo "$NEW_FAILURES"
      popd >/dev/null
      exit 1
    fi
  fi

  # On the LAST ordering, capture final diff for content invariant check
  if [ "$ORDERING_NUM" -eq "$TOTAL" ]; then
    git diff "$BASE...HEAD" >"$WORK_DIR/split-union.diff"
  fi

  popd >/dev/null
  git worktree remove "$WORKTREE" --force 2>/dev/null || true
done <"$WORK_DIR/orderings.txt"

echo ""
echo "=== All $TOTAL orderings merge cleanly ==="

# Content invariant check: union of splits must equal original diff
echo ""
echo "=== Verifying content invariant (split union = original diff) ==="

# Normalize diffs for comparison (strip commit hashes, dates, etc.)
normalize_diff() {
  # Remove index lines, timestamps, and normalize paths
  grep -v "^index " | grep -v "^@@" | sort
}

ORIG_NORMALIZED=$(normalize_diff <"$WORK_DIR/original.diff" | md5sum | cut -d' ' -f1)
SPLIT_NORMALIZED=$(normalize_diff <"$WORK_DIR/split-union.diff" | md5sum | cut -d' ' -f1)

if [ "$ORIG_NORMALIZED" = "$SPLIT_NORMALIZED" ]; then
  echo "✓ Content invariant PASSED: split union equals original diff"
else
  echo "FAIL: Content invariant violated!"
  echo ""
  echo "Original diff and split union differ. Checking what's missing..."
  echo ""

  # Show files in original but not in split
  ORIG_FILES=$(grep "^diff --git" "$WORK_DIR/original.diff" | sort)
  SPLIT_FILES=$(grep "^diff --git" "$WORK_DIR/split-union.diff" | sort)

  echo "Files in original: $(echo "$ORIG_FILES" | wc -l)"
  echo "Files in split union: $(echo "$SPLIT_FILES" | wc -l)"

  MISSING=$(comm -23 <(echo "$ORIG_FILES") <(echo "$SPLIT_FILES"))
  EXTRA=$(comm -13 <(echo "$ORIG_FILES") <(echo "$SPLIT_FILES"))

  if [ -n "$MISSING" ]; then
    echo ""
    echo "Files in original but MISSING from split:"
    echo "$MISSING"
  fi

  if [ -n "$EXTRA" ]; then
    echo ""
    echo "Files in split but NOT in original (unexpected):"
    echo "$EXTRA"
  fi

  # Save diffs for manual inspection
  echo ""
  echo "Diffs saved for inspection:"
  echo "  Original: $WORK_DIR/original.diff"
  echo "  Split union: $WORK_DIR/split-union.diff"

  # Don't delete work dir on failure
  trap - EXIT
  exit 1
fi

echo ""
echo "=== VALIDATION PASSED ==="
echo "- All $TOTAL orderings merge without conflicts"
if [ "$SKIP_TESTS" != "--skip-tests" ]; then
  echo "- No new test failures introduced"
fi
echo "- Content invariant verified (split = original)"
