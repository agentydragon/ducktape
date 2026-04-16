#!/usr/bin/env bash
# Test scaffold: builds the garbled victim binary from source, then runs the
# RE recipe against it.
#
# Env vars (set via Bazel env = {...}):
#   GARBLE — rlocation path to the garble go_binary
#   GO     — rlocation path to the go_bin_runner wrapper
#   RECIPE — rlocation path to garble_re_recipe.sh
set -euo pipefail

# ── 1. Resolve GOROOT via go_bin_runner ──────────────────────────────────────
# go_bin_runner needs third_party/go.mod to locate GOROOT.
# In bzlmod runfiles the workspace root is at $TEST_SRCDIR/_main, where
# _main/third_party/go.mod exists (declared as data dep).
mkdir -p "$TEST_TMPDIR/bin"
ln -s "$TEST_SRCDIR/$GO" "$TEST_TMPDIR/bin/go_bin_runner"

export GOROOT
GOROOT=$(cd "$TEST_SRCDIR/_main" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)

# Symlink the real go binary (not go_bin_runner) so garble and go tool work
# without needing workspace context.
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"

# ── 2. Build the garbled victim binary ───────────────────────────────────────
SRCDIR="$TEST_TMPDIR/src"
mkdir -p "$SRCDIR"

# Copy victim source files (filegroup srcs) from bzlmod runfiles layout.
VICTIM_DIR="$TEST_SRCDIR/_main/skills/reverse_engineer/cmd/garble_target"
for f in "$VICTIM_DIR"/*.go; do
  cp "$f" "$SRCDIR/"
done
printf 'module garble_target\ngo 1.26.0\n' >"$SRCDIR/go.mod"

export GOCACHE="$TEST_TMPDIR/.gocache"
export GOPATH="$TEST_TMPDIR/.gopath"
# garble uses XDG_CACHE_HOME (or HOME) for its own cache; set it explicitly
# since Bazel sandbox may not set HOME.
export XDG_CACHE_HOME="$TEST_TMPDIR/.xdg-cache"
mkdir -p "$XDG_CACHE_HOME"

# garble reads its own build ID as the default obfuscation seed; Bazel-built
# binaries have a short "redacted" build ID — garble is patched to pad it to
# 15 bytes instead of panicking. Use -seed=random for non-deterministic output
# that exercises the full obfuscation path.
GARBLE_BIN="$TEST_SRCDIR/$GARBLE"
(cd "$SRCDIR" && "$GARBLE_BIN" -seed=random build -o "$TEST_TMPDIR/garbled-binary" .)

# ── 3. Run the recipe in TEST_TMPDIR where 'garbled-binary' lives ────────────
cd "$TEST_TMPDIR"
exec bash "$TEST_SRCDIR/$RECIPE"
