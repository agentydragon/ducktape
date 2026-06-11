#!/bin/bash
# Workspace status script for Bazel stamping.
# Outputs key-value pairs used by --stamp builds.
# See: https://bazel.build/docs/user-manual#workspace-status

# Volatile: changes every build (timestamp, etc.) - not cached
# Stable: only changes when repo changes - cached

# Get git commit (short hash)
if command -v git &>/dev/null && git rev-parse --git-dir &>/dev/null; then
  COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  # Check for dirty working tree
  if ! git diff --quiet HEAD 2>/dev/null; then
    COMMIT="${COMMIT}-dirty"
  fi
else
  COMMIT="unknown"
fi

# Stable status - only changes when commit changes
echo "STABLE_BUILD_COMMIT ${COMMIT}"
