#!/usr/bin/env bash
# Verifies inline_strings recovers string constants that garble -literals cannot
# hide, including ones split across several compare instructions.
#
# The load-bearing assertion is `memprofilerate`: a GODEBUG key that Go's own
# runtime.parsegodebug compares inline, so it is present in any Go binary and
# stable across builds. At 14 bytes it cannot come from a single immediate --
# recovering it whole proves the displacement reassembly works, not merely that
# ASCII immediates were spotted.
#
# Env vars (set via Bazel env = {...}):
#   INLINE_STRINGS — rlocation path to the inline_strings script
#   V2_GARBLED     — rlocation path to the pre-built garble-obfuscated binary

set -euo pipefail

cd "$TEST_TMPDIR"
cp "$TEST_SRCDIR/$V2_GARBLED" garbled
chmod +w garbled

"$TEST_SRCDIR/$INLINE_STRINGS" garbled /dev/null >out.txt

total=$(wc -l <out.txt)
echo "== recovered $total inline strings =="
[ "$total" -ge 20 ] || {
  echo "FAIL: only $total strings" >&2
  cat out.txt >&2
  exit 1
}

echo "== multi-fragment reassembly =="
# Anything longer than 8 bytes had to be stitched from >1 immediate.
long=$(awk -F'\t' 'length($3) > 8' out.txt | wc -l)
[ "$long" -ge 1 ] || {
  echo "FAIL: no string longer than 8 bytes; reassembly did not fire" >&2
  cat out.txt >&2
  exit 1
}
echo "ok: $long strings longer than 8 bytes"

echo "== the known 14-byte constant must come back whole =="
grep -qP '\tmemprofilerate$' out.txt || {
  echo "FAIL: 'memprofilerate' not recovered exactly" >&2
  grep -i memprof out.txt >&2 || echo "(no memprof* fragment at all)" >&2
  exit 1
}
echo "ok: memprofilerate"

echo "== fragments must not bleed into neighbours =="
# A too-greedy stitcher concatenates unrelated constants; nothing legitimate in
# a fixture this small is anywhere near this long.
if awk -F'\t' 'length($3) > 80' out.txt | grep -q .; then
  echo "FAIL: over-long string suggests fragments were over-merged" >&2
  awk -F'\t' 'length($3) > 80' out.txt >&2
  exit 1
fi
echo "ok: no over-merged strings"

echo "PASS"
