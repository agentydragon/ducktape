#!/usr/bin/env bash
set -euo pipefail
wrapper="$(rlocation "$1")"
lean="$(rlocation "$2")"
source="$(rlocation "$3")"
cp "$source" "$TEST_TMPDIR/Sorry.lean"
"$lean" --root="$TEST_TMPDIR" -o "$TEST_TMPDIR/reference.olean" -c "$TEST_TMPDIR/reference.c" "$TEST_TMPDIR/Sorry.lean" >"$TEST_TMPDIR/reference.log" 2>&1
grep -F 'declaration uses ' "$TEST_TMPDIR/reference.log" >/dev/null
if "$wrapper" "$lean" "$TEST_TMPDIR" "$TEST_TMPDIR/Sorry.lean" "$TEST_TMPDIR/test.olean" "$TEST_TMPDIR/test.c" >"$TEST_TMPDIR/test.log" 2>&1; then
  echo 'Native Lean compiler accepted an admitted proof' >&2
  exit 1
fi
grep -F 'Lean module contains an admitted declaration' "$TEST_TMPDIR/test.log" >/dev/null
