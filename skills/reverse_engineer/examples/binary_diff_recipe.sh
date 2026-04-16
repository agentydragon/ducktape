#!/usr/bin/env bash
# Recipe: cross-version binary diffing on garble-obfuscated Go binaries.
#
# Demonstrates string-anchored function matching step-by-step against a real
# binary pair: v1_plain (symbols intact) and v2_garbled (names randomized,
# built with a fixed garble seed so all addresses and names are deterministic).
#
# Technique for the garbled binary:
#   string → file offset → .rodata VMA → instruction referencing that VMA
#   → instruction PC → pclntool maps PC to garbled function name
#
# For the reference binary (v1, has symbols), nm lists function names directly.
#
# Prerequisites:
#   - 'v1_plain'   in cwd — Go binary built without garble (has pclntab + symbols)
#   - 'v2_garbled' in cwd — same codebase (+ validateConfig) built with garble
#                           and a fixed seed for deterministic output
#   - 'objdump', 'readelf', 'nm', 'strings', 'go' on PATH
#   - 'pclntool' on PATH   (build with: go build -o pclntool pclntool.go)

set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# string_vma BINARY STRING
#   Compute the virtual address of STRING's bytes in .rodata.
#   Prints: file offset, .rodata bounds, and the resulting VMA.
string_vma() {
  local binary="$1" search="$2"
  local file_off sec_vma sec_file vma
  file_off=$(grep -obaF "$search" "$binary" | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || fail "'$search' not found in $binary"
  sec_vma=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  vma=$(printf "0x%x" $((sec_vma + file_off - sec_file)))
  echo "    file offset:     $file_off (decimal)"
  echo "    .rodata VMA:     $sec_vma"
  echo "    .rodata fileoff: $sec_file"
  echo "    string VMA:      $vma"
  echo "$vma"
}

# fn_at_vma BINARY VMA
#   Find the instruction referencing VMA and map its PC to a function name.
#   Prints the matching objdump line and the resolved function name.
fn_at_vma() {
  local binary="$1" vma="$2"
  local insn_line insn_addr fn
  insn_line=$(objdump -d "$binary" 2>/dev/null | grep -E "# ${vma}( <|$)" | head -1)
  [ -n "$insn_line" ] || fail "no instruction in $binary references VMA $vma"
  echo "    objdump line:    $insn_line"
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')
  echo "    instruction PC:  $insn_addr"
  fn=$(pclntool pc "$binary" "$insn_addr")
  echo "    garbled name:    $fn"
  echo "$fn"
}

# fn_for_string_v2 STRING
#   Full pipeline: string → VMA → instruction → garbled function name.
fn_for_string_v2() {
  local search="$1" vma fn
  vma=$(string_vma v2_garbled "$search" | tail -1)
  fn=$(fn_at_vma v2_garbled "$vma" | tail -1)
  echo "$fn"
}

echo "=== 1. Verify binary properties ==="
go version -m v1_plain 2>&1 | grep -q "garble_target" \
  || fail "v1_plain does not contain expected module info"
echo "v1_plain: symbols + module info intact"
go version -m v2_garbled 2>&1 | grep -q "unknown" \
  || fail "v2_garbled does not appear to be garbled"
echo "v2_garbled: module info stripped (garble confirmed)"

echo ""
echo "=== 2. v1 function inventory via nm ==="
echo "TEXT symbols in v1_plain (main package):"
nm v1_plain 2>/dev/null | awk '$2 == "T" && $3 ~ /^main\./ {print "  " $3}'

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
check_absent "main.validateConfig"
echo "  main.validateConfig absent from v1 (new in v2)"

echo ""
echo "=== 3. String-anchored function mapping in v2_garbled ==="

S1="connection refused: server not accepting connections"
echo "--- '$S1' ---"
fn1_v2=$(fn_for_string_v2 "$S1")

S2="failed to read config file"
echo "--- '$S2' ---"
fn2_v2=$(fn_for_string_v2 "$S2")

echo ""
echo "--- Summary so far ---"
echo "  '$S1' → $fn1_v2"
echo "  '$S2' → $fn2_v2"
[ "$fn1_v2" != "$fn2_v2" ] \
  || fail "connectToServer and loadConfig garbled to same name ($fn1_v2)"
echo "  PASS: two distinct source functions → two distinct garbled names"

echo ""
echo "=== 4. v2-only string identifies new function ==="

S_NEW="invalid port: must be between 1 and 65535"
echo "--- '$S_NEW' ---"
! grep -qaF "$S_NEW" v1_plain \
  || fail "'$S_NEW' found in v1_plain — expected only in v2"
echo "  absent from v1"
fn_new=$(fn_for_string_v2 "$S_NEW")

echo ""
echo "--- Summary ---"
echo "  '$S1' → $fn1_v2   (connectToServer)"
echo "  '$S2' → $fn2_v2   (loadConfig)"
echo "  '$S_NEW' → $fn_new   (validateConfig, v2-only)"
[ "$fn_new" != "$fn1_v2" ] \
  || fail "validateConfig collides with connectToServer garbled name"
[ "$fn_new" != "$fn2_v2" ] \
  || fail "validateConfig collides with loadConfig garbled name"
echo "  PASS: three distinct garbled names for three distinct source functions"

echo ""
echo "All assertions passed."
