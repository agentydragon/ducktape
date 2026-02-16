#!/bin/bash
# Configure BuildBuddy remote cache for Bazel if BUILDBUDDY_API_KEY is set.
#
# Writes config to ~/.config/bazel/buildbuddy.bazelrc which is loaded via
# try-import in ~/.bazelrc (set up by nix home-manager or this script in CI).
#
# Usage:
#   - Claude Code SessionStart hook
#   - GitHub Actions (via setup-buildbuddy action)
#   - Direct invocation

set -euo pipefail

if [[ -z "${BUILDBUDDY_API_KEY:-}" ]]; then
  exit 0
fi

# Write BuildBuddy config to standard location
BUILDBUDDY_BAZELRC="$HOME/.config/bazel/buildbuddy.bazelrc"
mkdir -p "$(dirname "$BUILDBUDDY_BAZELRC")"

cat >"$BUILDBUDDY_BAZELRC" <<EOF
# BuildBuddy authentication (auto-generated)
# Static configuration is in .bazelrc under build:rbe
common --remote_header=x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}

# Enable RBE (platforms, exec properties in .bazelrc + BUILD.bazel platform)
build --config=rbe
EOF

# Override the RBE container image if RBE_IMAGE is set (used by CI when
# testing an updated RBE image before it becomes :latest).
# remote_header platform overrides take precedence over platform exec_properties.
if [[ -n "${RBE_IMAGE:-}" ]]; then
  echo "build --remote_header=x-buildbuddy-platform.container-image=docker://${RBE_IMAGE}" >>"$BUILDBUDDY_BAZELRC"
  echo "RBE image override: $RBE_IMAGE"
fi

# Ensure ~/.bazelrc has the try-import (for CI environments without home-manager)
USER_BAZELRC="$HOME/.bazelrc"
if [[ ! -f "$USER_BAZELRC" ]] || ! grep -q "try-import.*buildbuddy.bazelrc" "$USER_BAZELRC" 2>/dev/null; then
  echo "" >>"$USER_BAZELRC"
  echo "# BuildBuddy remote cache (auto-added by setup_buildbuddy.sh)" >>"$USER_BAZELRC"
  echo "try-import $BUILDBUDDY_BAZELRC" >>"$USER_BAZELRC"
fi

echo "BuildBuddy remote cache configured at $BUILDBUDDY_BAZELRC"
