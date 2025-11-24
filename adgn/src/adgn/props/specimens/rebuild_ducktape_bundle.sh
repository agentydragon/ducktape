#!/bin/bash
# Rebuild the shared bundle for all ducktape specimens
# This script extracts specimen refs from manifests and creates a single bundle

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_PATH="$SCRIPT_DIR/ducktape-specimens.bundle"
REPO_ROOT="$(git rev-parse --show-toplevel)"

echo "Extracting ducktape specimen commits from manifests..."

# Find all ducktape specimen refs
REFS=()
for manifest in "$SCRIPT_DIR"/*/manifest.yaml; do
    if [ ! -f "$manifest" ]; then
        continue
    fi

    # Check if this is a ducktape specimen (either github source or git-bundle)
    if grep -q 'repo: ducktape' "$manifest" 2>/dev/null || \
       grep -q 'vcs: git-bundle' "$manifest" 2>/dev/null; then
        ref=$(grep 'ref:' "$manifest" | head -1 | sed 's/.*ref: *//' | tr -d ' ')
        if [ -n "$ref" ]; then
            specimen_name=$(basename "$(dirname "$manifest")")
            echo "  - $specimen_name: $ref"
            REFS+=("$ref")
        fi
    fi
done

if [ ${#REFS[@]} -eq 0 ]; then
    echo "No ducktape specimens found"
    exit 1
fi

echo ""
echo "Creating bundle with ${#REFS[@]} commits..."

# Verify all commits exist
missing=0
for ref in "${REFS[@]}"; do
    if ! git cat-file -e "$ref" 2>/dev/null; then
        echo "ERROR: Commit $ref not found in repository"
        echo "       You may need to fetch it from remote or checkout the branch"
        missing=1
    fi
done

if [ $missing -eq 1 ]; then
    echo ""
    echo "To fetch missing commits, try:"
    echo "  git fetch origin"
    echo "Or if commits are on local branches:"
    echo "  git fetch . branch-name"
    exit 1
fi

# Create bundle with all refs
# We need to create a bundle that includes all these commits
# The bundle format requires "rev-list args", so we just list the commits
echo "Writing bundle to: $BUNDLE_PATH"
git bundle create "$BUNDLE_PATH" "${REFS[@]}"

echo ""
echo "Verifying bundle..."
git bundle verify "$BUNDLE_PATH"

echo ""
echo "Bundle created successfully!"
echo "Size: $(du -h "$BUNDLE_PATH" | cut -f1)"
echo ""
echo "Bundle contains:"
git bundle list-heads "$BUNDLE_PATH"
