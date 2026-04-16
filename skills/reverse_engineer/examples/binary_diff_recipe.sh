#!/usr/bin/env bash
# Recipe: cross-version binary diffing on garble-obfuscated Go binaries.
#
# Demonstrates string-anchored function matching step-by-step against a real
# binary pair: v1_plain (symbols intact) and v2_garbled (names randomized,
# built with a fixed garble seed so all intermediate values are deterministic).
#
# Technique for the garbled binary:
#   string → file offset → .rodata VMA → instruction referencing that VMA
#   → instruction PC → pclntool maps PC to garbled function name
#
# Golden values (seed=ZHVja3RhcGU=, i.e. base64("ducktape")):
#   connectToServer  string VMA 0x51a851  PC 0x4dfccd  → main.h8n9KNzKUCmw
#   loadConfig       string VMA 0x5162ef  PC 0x4dfdc2  → main.bhY2uMqP4
#   validateConfig   string VMA 0x518f8a  PC 0x4dfb9c  → main.epq7tEoS
#
# Prerequisites:
#   - 'v1_plain'   in cwd — Go binary built without garble (has pclntab + symbols)
#   - 'v2_garbled' in cwd — same codebase (+ validateConfig) built with garble
#                           using a fixed seed for deterministic output
#   - 'objdump', 'readelf', 'nm', 'go' on PATH
#   - 'pclntool' on PATH   (build with: go build -o pclntool pclntool.go)

set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [ "$got" != "$want" ]; then
    fail "$label: expected '$want', got '$got'"
  fi
  echo "  ASSERT OK: $label = '$got'"
}

# string_vma BINARY STRING EXPECTED_VMA
#   stdout: hex VMA; stderr: step trace; asserts VMA matches expected
string_vma() {
  local binary="$1" search="$2" expected_vma="$3"
  local file_off sec_vma sec_file vma
  file_off=$(grep -obaF "$search" "$binary" | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || fail "'$search' not found in $binary"
  sec_vma=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S "$binary" | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  vma=$(printf "0x%x" $((sec_vma + file_off - sec_file)))
  echo "  string file offset: $file_off" >&2
  echo "  .rodata VMA:        $sec_vma  file offset: $sec_file" >&2
  echo "  string VMA:         $vma" >&2
  assert_eq "string VMA for '$search'" "$vma" "$expected_vma" >&2
  echo "$vma"
}

# fn_at_vma BINARY VMA EXPECTED_PC EXPECTED_BYTES EXPECTED_FN
#   stdout: garbled fn name; stderr: step trace; asserts PC, bytes, fn match expected
fn_at_vma() {
  local binary="$1" vma="$2" expected_pc="$3" expected_bytes="$4" expected_fn="$5"
  local insn_line insn_addr fn
  insn_line=$(objdump -d "$binary" 2>/dev/null | grep -E "# ${vma}( <|$)" | head -1)
  [ -n "$insn_line" ] || fail "no instruction in $binary references VMA $vma"
  echo "  objdump:            $insn_line" >&2
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')
  echo "  instruction PC:     $insn_addr" >&2
  assert_eq "instruction PC for VMA $vma" "$insn_addr" "$expected_pc" >&2
  # Check instruction bytes (space-separated hex from the objdump line)
  insn_bytes=$(echo "$insn_line" | awk '{
    s = ""; for (i=2; i<=8; i++) { if ($i ~ /^[0-9a-f][0-9a-f]$/) s = s (s ? " " : "") $i }; print s}')
  echo "  instruction bytes:  $insn_bytes" >&2
  assert_eq "instruction bytes for VMA $vma" "$insn_bytes" "$expected_bytes" >&2
  fn=$(pclntool pc "$binary" "$insn_addr")
  echo "  garbled name:       $fn" >&2
  assert_eq "garbled name for VMA $vma" "$fn" "$expected_fn" >&2
  echo "$fn"
}

fn_for_string_v2() {
  local vma
  vma=$(string_vma v2_garbled "$1" "$2")
  fn_at_vma v2_garbled "$vma" "$3" "$4" "$5"
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
  nm v1_plain 2>/dev/null | awk -v s="$1" '$3 == s {found=1} END {exit !found}' \
    || fail "$1 not in v1 symbol table"
}
check_absent() {
  nm v1_plain 2>/dev/null | awk -v s="$1" '$3 == s {found=1} END {exit found}' \
    || fail "$1 unexpectedly found in v1 symbol table"
}

check_present "main.connectToServer"
check_present "main.loadConfig"
check_absent "main.validateConfig"
echo "  main.validateConfig absent from v1 (new in v2)"

echo ""
echo "=== 3. String-anchored function mapping in v2_garbled ==="
# Golden values are deterministic for seed ZHVja3RhcGU= (base64 "ducktape").

S1="connection refused: server not accepting connections"
echo "Locating '$S1':"
# Expected: string VMA 0x51a851, lea at PC 0x4dfccd, bytes 48 8d 05 7d ab 03 00
fn1_v2=$(fn_for_string_v2 "$S1" "0x51a851" "0x4dfccd" "48 8d 05 7d ab 03 00" "main.h8n9KNzKUCmw")
echo "  → $fn1_v2"

echo ""
S2="failed to read config file"
echo "Locating '$S2':"
# Expected: string VMA 0x5162ef, lea at PC 0x4dfdc2, bytes 48 8d 05 26 65 03 00
fn2_v2=$(fn_for_string_v2 "$S2" "0x5162ef" "0x4dfdc2" "48 8d 05 26 65 03 00" "main.bhY2uMqP4")
echo "  → $fn2_v2"

echo ""
[ "$fn1_v2" != "$fn2_v2" ] \
  || fail "connectToServer and loadConfig garbled to same name ($fn1_v2)"
echo "PASS: distinct source functions → distinct garbled names"

echo ""
echo "=== 4. v2-only string identifies new function ==="

S_NEW="invalid port: must be between 1 and 65535"
! grep -qaF "$S_NEW" v1_plain \
  || fail "'$S_NEW' found in v1_plain — expected only in v2"
echo "'$S_NEW': absent from v1 (validateConfig only exists in v2)"
echo "Locating '$S_NEW' in v2_garbled:"
# Expected: string VMA 0x518f8a, lea at PC 0x4dfb9c, bytes 48 8d 05 e7 93 03 00
fn_new=$(fn_for_string_v2 "$S_NEW" "0x518f8a" "0x4dfb9c" "48 8d 05 e7 93 03 00" "main.epq7tEoS")
echo "  → $fn_new"

echo ""
[ "$fn_new" != "$fn1_v2" ] \
  || fail "validateConfig collides with connectToServer garbled name"
[ "$fn_new" != "$fn2_v2" ] \
  || fail "validateConfig collides with loadConfig garbled name"
echo "PASS: three distinct garbled names for three distinct source functions"

echo ""
echo "=== Summary ==="
echo "  connectToServer  string VMA 0x51a851  PC 0x4dfccd  →  $fn1_v2"
echo "  loadConfig       string VMA 0x5162ef  PC 0x4dfdc2  →  $fn2_v2"
echo "  validateConfig   string VMA 0x518f8a  PC 0x4dfb9c  →  $fn_new  (v2-only)"

echo ""
echo "All assertions passed."
