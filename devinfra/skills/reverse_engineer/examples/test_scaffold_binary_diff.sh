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
ln -s "$TEST_SRCDIR/$PCLNTOOL" "$TEST_TMPDIR/bin/pclntool"

# 'go version -m' is used by the recipe to verify binary properties.
ln -s "$TEST_SRCDIR/$GO" "$TEST_TMPDIR/bin/go_bin_runner"
export GOROOT
GOROOT=$(cd "$TEST_SRCDIR/_main" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"

# Symlink pre-built binaries into cwd (recipe uses bare names v1_plain/v2_garbled).
ln -s "$TEST_SRCDIR/$V1_PLAIN" "$TEST_TMPDIR/v1_plain"
ln -s "$TEST_SRCDIR/$V2_GARBLED" "$TEST_TMPDIR/v2_garbled"

cd "$TEST_TMPDIR"
exec bash "$TEST_SRCDIR/$RECIPE"
