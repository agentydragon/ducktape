#!/usr/bin/env bash
# Recipe: cross-version binary diffing on garble-obfuscated Go binaries.
#
# Demonstrates string-anchored function matching: given a plain binary (v1,
# symbols intact) and a garble-obfuscated binary (v2, names randomized), shared
# string literals identify the same logical function across versions. v2-only
# strings identify newly added functions with no v1 counterpart.
#
# The technique for the garbled binary:
#   string → file offset → .rodata VMA → instruction referencing that VMA
#   → pclntool maps the instruction PC to its garbled function name
#
# For the reference binary (v1, has symbols), nm directly lists function names.
# Shared strings confirm both binaries' functions correspond to the same source.
#
# Prerequisites:
#   - 'v1_plain'   in cwd — Go binary built without garble (has pclntab + symbols)
#   - 'v2_garbled' in cwd — updated codebase built with garble (no symbols)
#   - 'objdump', 'readelf', 'nm', 'strings' on PATH
#   - 'pclntool' on PATH   (build with: go build -o pclntool pclntool.go)
#   - 'go' on PATH         (for 'go version -m')

set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# string_vma BINARY STRING → hex VMA of STRING's bytes in .rodata
string_vma() {
  local binary="$1" search="$2"
  local file_off sec_vma sec_file
  file_off=$(grep -obaF "$search" "$binary" | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || fail "'$search' not found in $binary"
  sec_vma=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  printf "0x%x" $((sec_vma + file_off - sec_file))
}

# fn_at_vma BINARY VMA → garbled function name via pclntool.
#
# Garbled (PIE) binaries use RIP-relative string references; objdump annotates
# them with '# VMA'. Plain binaries may use absolute or GOT-based addressing,
# so this technique is not reliable for non-garbled binaries.
fn_at_vma() {
  local binary="$1" vma="$2"
  local insn_line insn_addr
  insn_line=$(objdump -d "$binary" 2>/dev/null | grep -E "# ${vma}( <|$)" | head -1)
  [ -n "$insn_line" ] || fail "no instruction in $binary references VMA $vma"
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')
  pclntool pc "$binary" "$insn_addr"
}

# fn_for_string_v2 STRING → garbled function name in v2_garbled containing STRING
fn_for_string_v2() {
  fn_at_vma v2_garbled "$(string_vma v2_garbled "$1")"
}

echo "=== 1. Verify binary properties ==="

go version -m v1_plain 2>&1 | grep -q "garble_target" \
  || fail "v1_plain does not contain expected module info"
echo "v1_plain: symbols + module info intact"

go version -m v2_garbled 2>&1 | grep -q "unknown" \
  || fail "v2_garbled does not appear to be garbled"
echo "v2_garbled: module info stripped (garble confirmed)"

echo ""
echo "=== 2. v1 function inventory via nm (establishes ground truth) ==="
# nm lists all TEXT symbols in the plain binary — a direct name roster.
echo "TEXT symbols in v1_plain (main package):"
nm v1_plain 2>/dev/null | awk '$2 == "T" && $3 ~ /^main\./ {print "  " $3}'

# Use awk for symbol checks (consistent with how we extracted $3 above).
check_present() {
  local sym="$1"
  nm v1_plain 2>/dev/null | awk -v s="$sym" '$3 == s {found=1} END {exit !found}' \
    || fail "$sym not in v1 symbol table"
}
check_absent() {
  local sym="$1"
  nm v1_plain 2>/dev/null | awk -v s="$sym" '$3 == s {found=1} END {exit found}' \
    || fail "$sym unexpectedly found in v1 symbol table"
}

check_present "main.connectToServer"
check_present "main.loadConfig"

# validateConfig is new in v2 — must NOT be in v1.
check_absent "main.validateConfig"
echo "  main.validateConfig absent from v1 (as expected — added in v2)"

echo ""
echo "=== 3. v2 function names via string anchoring ==="
# 'connection refused' is in connectToServer (both v1 and v2).
# 'failed to read config file' is in loadConfig (both v1 and v2).
# 'invalid port' is in validateConfig (v2 only).

S1="connection refused: server not accepting connections"
S2="failed to read config file"
S_NEW="invalid port: must be between 1 and 65535"

fn1_v2=$(fn_for_string_v2 "$S1")
fn2_v2=$(fn_for_string_v2 "$S2")
echo "  '$S1'"
echo "  → v2 garbled fn: $fn1_v2  (corresponds to main.connectToServer)"

echo "  '$S2'"
echo "  → v2 garbled fn: $fn2_v2  (corresponds to main.loadConfig)"

# Two distinct source functions → two distinct garbled names.
[ "$fn1_v2" != "$fn2_v2" ] \
  || fail "connectToServer and loadConfig garbled to the same name ($fn1_v2)"
echo "  PASS: two distinct source functions → two distinct garbled names"

echo ""
echo "=== 4. v2-only string → new function with no v1 counterpart ==="

! grep -qaF "$S_NEW" v1_plain \
  || fail "'$S_NEW' found in v1_plain — expected only in v2"
echo "  '$S_NEW' absent from v1 (validateConfig only exists in v2)"

fn_new=$(fn_for_string_v2 "$S_NEW")
echo "  → v2 garbled fn: $fn_new  (new validateConfig, no v1 counterpart)"

[ "$fn_new" != "$fn1_v2" ] \
  || fail "validateConfig collides with connectToServer garbled name ($fn1_v2)"
[ "$fn_new" != "$fn2_v2" ] \
  || fail "validateConfig collides with loadConfig garbled name ($fn2_v2)"
echo "  PASS: three distinct garbled names for three distinct source functions"

echo ""
echo "All assertions passed."
