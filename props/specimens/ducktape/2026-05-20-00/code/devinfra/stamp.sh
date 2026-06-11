#!/usr/bin/env bash
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

# Get commit timestamp (ISO 8601, deterministic per commit)
if command -v git &>/dev/null && git rev-parse --git-dir &>/dev/null; then
  COMMIT_TIME=$(git log -1 --format=%cI HEAD 2>/dev/null || echo "unknown")
else
  COMMIT_TIME="unknown"
fi

# Stable status - only changes when commit changes
echo "STABLE_BUILD_COMMIT ${COMMIT}"
echo "STABLE_BUILD_TIMESTAMP ${COMMIT_TIME}"

# Volatile status — BuildBuddy reads these for UI metadata
# (repo links, branch display, user attribution)
echo "REPO_URL https://github.com/agentydragon/ducktape"
echo "BRANCH_NAME $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "COMMIT_SHA $(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo "USER $(whoami 2>/dev/null || echo unknown)"
echo "HOST $(hostname 2>/dev/null || echo unknown)"
