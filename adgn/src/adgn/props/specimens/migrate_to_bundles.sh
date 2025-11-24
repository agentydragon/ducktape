#!/bin/bash
# Migrate ducktape specimen manifests from github source to bundle source
# This updates manifests to reference the shared ducktape-specimens.bundle

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_NAME="ducktape-specimens.bundle"

echo "Migrating ducktape specimen manifests to use bundle source..."
echo ""

count=0
for manifest in "$SCRIPT_DIR"/*/manifest.yaml; do
    if [ ! -f "$manifest" ]; then
        continue
    fi

    # Check if this is a ducktape specimen
    if ! grep -q 'repo: ducktape' "$manifest" 2>/dev/null; then
        continue
    fi

    # Extract the current ref
    ref=$(grep 'ref:' "$manifest" | head -1 | sed 's/.*ref: *//' | tr -d ' ')
    if [ -z "$ref" ]; then
        echo "WARNING: No ref found in $manifest, skipping"
        continue
    fi

    specimen_name=$(basename "$(dirname "$manifest")")
    echo "Migrating $specimen_name (ref: $ref)"

    # Create backup
    cp "$manifest" "$manifest.bak"

    # Replace the source section
    # We'll use awk to replace the source section while preserving the scope section
    awk '
    BEGIN { in_source = 0; printed_new_source = 0 }
    /^source:/ {
        in_source = 1
        print "source:"
        print "  vcs: git-bundle"
        print "  path: ../'$BUNDLE_NAME'"
        print "  ref: '$ref'"
        printed_new_source = 1
        next
    }
    /^scope:/ {
        in_source = 0
        print ""
        print $0
        next
    }
    {
        if (!in_source) {
            print $0
        }
    }
    ' "$manifest.bak" > "$manifest"

    count=$((count + 1))
done

echo ""
echo "Migrated $count specimen(s)"
echo "Backup files saved as *.bak"
echo ""
echo "Next steps:"
echo "  1. Run ./rebuild_ducktape_bundle.sh to create the bundle"
echo "  2. Review the changes with: git diff"
echo "  3. Test a specimen: adgn-props run --specimen <name>"
echo "  4. If satisfied, remove backups: rm */manifest.yaml.bak"
