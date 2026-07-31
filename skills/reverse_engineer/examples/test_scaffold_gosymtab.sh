#!/usr/bin/env bash
# Verifies gosymtab restores a usable ELF symbol table on a garble-obfuscated
# Go binary — i.e. that `go tool objdump`, which refuses the garbled input with
# "no symbol section", works against the repaired output and resolves names.
#
# Env vars (set via Bazel env = {...}):
#   GO         — rlocation path to go_bin_runner
#   GOSYMTAB   — rlocation path to the gosymtab binary
#   V2_GARBLED — rlocation path to the pre-built garble-obfuscated binary

set -euo pipefail

mkdir -p "$TEST_TMPDIR/bin"
ln -s "$TEST_SRCDIR/$GO" "$TEST_TMPDIR/bin/go_bin_runner"
export GOROOT
GOROOT=$(cd "$TEST_SRCDIR/_main" && "$TEST_TMPDIR/bin/go_bin_runner" env GOROOT)
ln -s "$GOROOT/bin/go" "$TEST_TMPDIR/bin/go"
export PATH="$TEST_TMPDIR/bin:$PATH"
# The sandbox gives the test no HOME, and `go tool` refuses to run without a
# build cache location.
export HOME="$TEST_TMPDIR/home"
export GOCACHE="$TEST_TMPDIR/gocache"
mkdir -p "$HOME" "$GOCACHE"

cp "$TEST_SRCDIR/$V2_GARBLED" "$TEST_TMPDIR/garbled"
chmod +w "$TEST_TMPDIR/garbled"
cd "$TEST_TMPDIR"

echo "== baseline: go tool objdump must FAIL on the garbled binary =="
if go tool objdump -s '^main\.main$' garbled >/dev/null 2>baseline.err; then
  echo "FAIL: expected 'go tool objdump' to reject the garbled binary" >&2
  exit 1
fi
grep -q "no symbol section" baseline.err || {
  echo "FAIL: expected 'no symbol section', got:" >&2
  cat baseline.err >&2
  exit 1
}
echo "ok: rejected with 'no symbol section'"

echo "== repair =="
"$TEST_SRCDIR/$GOSYMTAB" garbled garbled.sym

echo "== the repaired binary must be a valid ELF with symbols =="
go tool nm garbled.sym >nm.out
nsyms=$(wc -l <nm.out)
[ "$nsyms" -gt 100 ] || {
  echo "FAIL: only $nsyms symbols recovered" >&2
  exit 1
}
echo "ok: $nsyms symbols"

echo "== objdump must now disassemble main.main =="
go tool objdump -s '^main\.main$' garbled.sym >main.asm
grep -q '^TEXT main\.main' main.asm || {
  echo "FAIL: main.main not disassembled" >&2
  head -20 main.asm >&2
  exit 1
}
echo "ok: main.main disassembled ($(wc -l <main.asm) lines)"

echo "== call targets must resolve to names, not bare addresses =="
# A correct symbol table lets objdump name CALL destinations. A symbol table
# built on the wrong text base still disassembles but resolves almost nothing.
resolved=$(grep -c 'CALL .*(SB)' main.asm || true)
[ "$resolved" -gt 0 ] || {
  echo "FAIL: no CALL resolved to a symbol" >&2
  exit 1
}
echo "ok: $resolved CALLs resolved to symbol names"

echo "== recovered names must include runtime symbols =="
grep -q '^ *[0-9a-f]* T runtime\.' nm.out || {
  echo "FAIL: no runtime.* symbols recovered" >&2
  exit 1
}
echo "ok: runtime symbols present"

echo "PASS"
