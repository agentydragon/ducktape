#!/usr/bin/env bash
# Test scaffold for garble_re_recipe.sh.
# Receives a pre-built garbled binary (built by Bazel genrule) and runs the
# recipe against it.
#
# Env vars (set via Bazel env = {...}):
#   BINARY  — rlocation path to the pre-built garbled binary
#   GO      — rlocation path to the go_bin_runner wrapper
#   RECIPE  — rlocation path to garble_re_recipe.sh
set -euo pipefail

# go_bin_runner needs third_party/go.mod to locate GOROOT.
# In bzlmod runfiles the workspace root is at $TEST_SRCDIR/_main, where
# _main/third_party/go.mod exists (declared as data dep).
mkdir -p "$TEST_TMPDIR/bin"
ln -s "$TEST_SRCDIR/$GO" "$TEST_TMPDIR/bin/go_bin_runner"

export GOROOT
GOROOT=$(cd "$TEST_SRCDIR/_main" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)

# Symlink the real go binary (not go_bin_runner) so that go tool addr2line
# works without needing workspace context.
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"

# Place the pre-built garbled binary where the recipe expects it.
cp "$TEST_SRCDIR/$BINARY" "$TEST_TMPDIR/garbled-binary"
chmod +x "$TEST_TMPDIR/garbled-binary"

# Run the recipe in TEST_TMPDIR where 'garbled-binary' lives.
cd "$TEST_TMPDIR"
exec bash "$TEST_SRCDIR/$RECIPE"
