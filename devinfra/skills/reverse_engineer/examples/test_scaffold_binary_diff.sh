#!/usr/bin/env bash
# Test scaffold for binary_diff_recipe.sh.
#
# Receives pre-built v1_plain and v2_garbled as Bazel runfiles (genrule outputs),
# sets up go and pclntool on PATH, then runs the recipe.
#
# Env vars (set via Bazel env = {...}):
#   GO        — rlocation path to go_bin_runner
#   PCLNTOOL  — rlocation path to pclntool
#   RECIPE    — rlocation path to binary_diff_recipe.sh
#   V1_PLAIN  — rlocation path to pre-built v1_plain binary
#   V2_GARBLED — rlocation path to pre-built v2_garbled binary

set -euo pipefail

mkdir -p "$TEST_TMPDIR/bin"
ln -s "$(rlocation "$PCLNTOOL")" "$TEST_TMPDIR/bin/pclntool"

# 'go version -m' is used by the recipe to verify binary properties.
ln -s "$(rlocation "$GO")" "$TEST_TMPDIR/bin/go_bin_runner"
export GOROOT
workspace_root="$(dirname "$(dirname "$(rlocation "$GO_MOD")")")"
GOROOT=$(cd "$workspace_root" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"

# Symlink pre-built binaries into cwd (recipe uses bare names v1_plain/v2_garbled).
ln -s "$(rlocation "$V1_PLAIN")" "$TEST_TMPDIR/v1_plain"
ln -s "$(rlocation "$V2_GARBLED")" "$TEST_TMPDIR/v2_garbled"

cd "$TEST_TMPDIR"
exec bash "$(rlocation "$RECIPE")"
