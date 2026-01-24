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
# BuildBuddy remote cache configuration (auto-generated)
build --bes_results_url=https://app.buildbuddy.io/invocation/
build --bes_backend=grpcs://remote.buildbuddy.io
common --remote_cache=grpcs://remote.buildbuddy.io
common --remote_timeout=10m
common --remote_header=x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}
common --experimental_remote_cache_compression
common --experimental_remote_cache_compression_threshold=100
build --noslim_profile
build --experimental_profile_include_target_label
build --experimental_profile_include_primary_output
build --nolegacy_important_outputs
EOF

# Ensure ~/.bazelrc has the try-import (for CI environments without home-manager)
USER_BAZELRC="$HOME/.bazelrc"
if [[ ! -f "$USER_BAZELRC" ]] || ! grep -q "try-import.*buildbuddy.bazelrc" "$USER_BAZELRC" 2>/dev/null; then
  echo "" >>"$USER_BAZELRC"
  echo "# BuildBuddy remote cache (auto-added by setup-buildbuddy.sh)" >>"$USER_BAZELRC"
  echo "try-import $BUILDBUDDY_BAZELRC" >>"$USER_BAZELRC"
fi

echo "BuildBuddy remote cache configured at $BUILDBUDDY_BAZELRC"
