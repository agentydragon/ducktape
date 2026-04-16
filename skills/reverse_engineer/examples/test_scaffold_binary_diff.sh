#!/usr/bin/env bash
# Test scaffold for binary_diff_recipe.sh.
#
# Builds v1_plain (symbols intact) from garble_target and v2_garbled
# (obfuscated) from garble_target_v2, then runs the recipe.
#
# Env vars (set via Bazel env = {...}):
#   GARBLE    — rlocation path to garble binary
#   GO        — rlocation path to go_bin_runner
#   PCLNTOOL  — rlocation path to pclntool
#   RECIPE    — rlocation path to binary_diff_recipe.sh

set -euo pipefail

mkdir -p "$TEST_TMPDIR/bin"
ln -s "$TEST_SRCDIR/$GO" "$TEST_TMPDIR/bin/go_bin_runner"
ln -s "$TEST_SRCDIR/$PCLNTOOL" "$TEST_TMPDIR/bin/pclntool"

export GOROOT
GOROOT=$(cd "$TEST_SRCDIR/_main" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"

export GOCACHE="$TEST_TMPDIR/.gocache"
export GOPATH="$TEST_TMPDIR/.gopath"
export XDG_CACHE_HOME="$TEST_TMPDIR/.xdg-cache"
mkdir -p "$XDG_CACHE_HOME"

GARBLE_BIN="$TEST_SRCDIR/$GARBLE"

# ── Build v1_plain (no garble, symbols + module info intact) ─────────────────
V1SRC="$TEST_TMPDIR/v1_src"
mkdir -p "$V1SRC"
V1_DIR="$TEST_SRCDIR/_main/skills/reverse_engineer/cmd/garble_target"
for f in "$V1_DIR"/*.go; do cp "$f" "$V1SRC/"; done
printf 'module garble_target\ngo 1.26.0\n' >"$V1SRC/go.mod"
(cd "$V1SRC" && go build -o "$TEST_TMPDIR/v1_plain" .)

# ── Build v2_garbled (same codebase + validateConfig, obfuscated by garble) ──
V2SRC="$TEST_TMPDIR/v2_src"
mkdir -p "$V2SRC"
V2_DIR="$TEST_SRCDIR/_main/skills/reverse_engineer/cmd/garble_target_v2"
for f in "$V2_DIR"/*.go; do cp "$f" "$V2SRC/"; done
printf 'module garble_target\ngo 1.26.0\n' >"$V2SRC/go.mod"
(cd "$V2SRC" && "$GARBLE_BIN" -seed=random build -o "$TEST_TMPDIR/v2_garbled" .)

cd "$TEST_TMPDIR"
exec bash "$TEST_SRCDIR/$RECIPE"
