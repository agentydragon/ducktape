#!/usr/bin/env bash
# Verifies gotypes recovers struct layouts from a garble-obfuscated binary.
#
# The fixture declares:
#
#   type ServerConfig struct {
#       Host  string `json:"host"`
#       Port  int    `json:"port"`
#       Token string `json:"token,omitempty"`
#   }
#
# garble randomizes the type name and all three field names. It cannot touch the
# struct tags, because encoding/json reads them reflectively at run time — which
# is exactly what makes this technique work, so the test asserts both halves:
# the names are gone, and the tags and offsets are intact.
#
# Env vars (set via Bazel env = {...}):
#   GOTYPES    — rlocation path to the gotypes binary
#   V2_GARBLED — rlocation path to the pre-built garble-obfuscated binary

set -euo pipefail

cp "$TEST_SRCDIR/$V2_GARBLED" "$TEST_TMPDIR/garbled"
chmod +w "$TEST_TMPDIR/garbled"
cd "$TEST_TMPDIR"

echo "== dump types =="
"$TEST_SRCDIR/$GOTYPES" garbled >types.txt 2>types.err
cat types.err
grep -q 'types base 0x' types.err || {
  echo "FAIL: expected gotypes to report a detected types base" >&2
  exit 1
}

echo "== the struct tags must survive garbling =="
for tag in 'json:"host"' 'json:"port"' 'json:"token,omitempty"'; do
  grep -qF "$tag" types.txt || {
    echo "FAIL: tag $tag not recovered" >&2
    echo "--- dump ---" >&2
    head -40 types.txt >&2
    exit 1
  }
  echo "ok: $tag"
done

# All three belong to one struct, so they must appear inside a single decl.
echo "== they must belong to the same struct, with correct offsets =="
awk '/^type .* struct \{/{buf=""; inb=1} inb{buf=buf $0 "\n"} /^\}/{if(inb && buf ~ /json:"host"/) print buf; inb=0}' \
  types.txt >serverconfig.txt
[ -s serverconfig.txt ] || {
  echo "FAIL: no single struct carries all the tags" >&2
  exit 1
}
cat serverconfig.txt

# string=16 bytes, int=8: Host +0x0, Port +0x10, Token +0x18.
grep -q 'json:"host".*+0x0' serverconfig.txt || {
  echo "FAIL: host offset" >&2
  exit 1
}
grep -q 'json:"port".*+0x10' serverconfig.txt || {
  echo "FAIL: port offset" >&2
  exit 1
}
grep -q 'json:"token,omitempty".*+0x18' serverconfig.txt || {
  echo "FAIL: token offset" >&2
  exit 1
}
echo "ok: field offsets match the Go layout"

echo "== and the identifiers really are garbled (otherwise this proves nothing) =="
if grep -q 'ServerConfig' serverconfig.txt; then
  echo "FAIL: type name survived; fixture is not obfuscated, test is vacuous" >&2
  exit 1
fi
if grep -qE '\bHost\b|\bToken\b' serverconfig.txt; then
  echo "FAIL: field names survived; fixture is not obfuscated" >&2
  exit 1
fi
echo "ok: names are randomized, tags and offsets are not"

echo "== -filter must narrow the output =="
name=$(awk '/json:"host"/{print prev} {prev=$0}' types.txt | head -1 \
  | sed -E 's/^type ([^ ]+) struct.*/\1/')
[ -n "$name" ] || name=$(grep -B20 'json:"host"' types.txt | grep '^type ' | tail -1 \
  | sed -E 's/^type ([^ ]+) struct.*/\1/')
"$TEST_SRCDIR/$GOTYPES" -filter "$name" garbled >filtered.txt 2>/dev/null
grep -qF 'json:"host"' filtered.txt || {
  echo "FAIL: -filter dropped the match" >&2
  exit 1
}
[ "$(grep -c '^type ' filtered.txt)" -lt "$(grep -c '^type ' types.txt)" ] || {
  echo "FAIL: -filter did not narrow anything" >&2
  exit 1
}
echo "ok: -filter narrows to the named type"

echo "PASS"
