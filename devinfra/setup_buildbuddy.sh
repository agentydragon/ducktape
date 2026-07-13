#!/bin/bash
# Configure BuildBuddy Bazel credentials if BUILDBUDDY_API_KEY is set.
#
# Writes config to ~/.config/bazel/buildbuddy.bazelrc which is loaded via
# try-import in ~/.bazelrc (set up by nix home-manager or this script in CI).
#
# Used by Codex Cloud and GitHub Actions setup flows.

set -euo pipefail

if [[ -z "${BUILDBUDDY_API_KEY:-}" ]]; then
  exit 0
fi

# Write BuildBuddy config to standard location
BUILDBUDDY_BAZELRC="$HOME/.config/bazel/buildbuddy.bazelrc"
mkdir -p "$(dirname "$BUILDBUDDY_BAZELRC")"

cat >"$BUILDBUDDY_BAZELRC" <<EOF
# BuildBuddy credential (auto-generated). Repositories opt into the rbe config.
common:rbe --remote_header=x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}
EOF

# Override RBE container image if RBE_IMAGE is set (used by CI when
# testing an updated RBE image before it becomes :latest).
# remote_header platform overrides take precedence over platform exec_properties.
if [[ -n "${RBE_IMAGE:-}" ]]; then
  echo "build:rbe --remote_header=x-buildbuddy-platform.container-image=docker://${RBE_IMAGE}" >>"$BUILDBUDDY_BAZELRC"
  echo "RBE image override: $RBE_IMAGE"
fi

# Ensure ~/.bazelrc has the try-import (for CI environments without home-manager)
USER_BAZELRC="$HOME/.bazelrc"
if [[ ! -f "$USER_BAZELRC" ]] || ! grep -q "try-import.*buildbuddy.bazelrc" "$USER_BAZELRC" 2>/dev/null; then
  echo "" >>"$USER_BAZELRC"
  echo "# BuildBuddy credentials (auto-added by setup_buildbuddy.sh)" >>"$USER_BAZELRC"
  echo "try-import $BUILDBUDDY_BAZELRC" >>"$USER_BAZELRC"
fi

echo "BuildBuddy Bazel credentials configured at $BUILDBUDDY_BAZELRC"
