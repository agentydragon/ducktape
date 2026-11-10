#!/usr/bin/env bash
# Recipe: cross-version binary diffing on garble-obfuscated Go binaries.
#
# Demonstrates string-anchored function matching step-by-step against a real
# binary pair: v1_plain (symbols intact) and v2_garbled (names randomized,
# built with a fixed garble seed so all intermediate values are deterministic).
#
# Technique for the garbled binary:
#   string → file offset → load-segment VMA → instruction referencing that VMA
#   → instruction PC → pclntool maps PC to garbled function name
#
# The linker may move strings and functions between Go releases, so this
# follows each string's actual address and reference in the fixture binaries.
# It also maps the plain-binary references back to their named source functions
# before checking that garbling produces three distinct names.
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

# string_vma BINARY STRING
#   stdout: hex VMA; stderr: step trace
string_vma() {
  local binary="$1" search="$2"
  local file_off load_file load_vma load_size vma
  file_off=$(grep -obaF "$search" "$binary" | head -1 | cut -d: -f1 || true)
  [ -n "$file_off" ] || fail "'$search' not found in $binary"
  vma=""
  while read -r load_file load_vma load_size; do
    if ((file_off >= load_file && file_off < load_file + load_size)); then
      vma=$(printf "0x%x" $((load_vma + file_off - load_file)))
      break
    fi
  done < <(readelf -lW "$binary" | awk '$1 == "LOAD" { print $2, $3, $5 }')
  [ -n "$vma" ] || fail "'$search' is outside file-backed PT_LOAD segments in $binary"
  echo "  string file offset: $file_off" >&2
  echo "  PT_LOAD VMA:        $load_vma  file offset: $load_file" >&2
  echo "  string VMA:         $vma" >&2
  echo "$vma"
}

# fn_at_vma BINARY VMA
#   stdout: function name; stderr: step trace
fn_at_vma() {
  local binary="$1" vma="$2"
  local insn_line insn_addr insn_bytes fn
  insn_line=$(objdump -d "$binary" 2>/dev/null \
    | grep -E "# (0x)?${vma#0x}([[:space:]]|$)" | head -1 || true)
  [ -n "$insn_line" ] || fail "no instruction in $binary references VMA $vma"
  echo "  objdump:            $insn_line" >&2
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')
  echo "  instruction PC:     $insn_addr" >&2
  insn_bytes=$(echo "$insn_line" | awk '{
    s = ""; for (i=2; i<=8; i++) { if ($i ~ /^[0-9a-f][0-9a-f]$/) s = s (s ? " " : "") $i }; print s}')
  [ -n "$insn_bytes" ] || fail "could not parse instruction bytes for VMA $vma"
  echo "  instruction bytes:  $insn_bytes" >&2
  fn=$(pclntool pc "$binary" "$insn_addr")
  echo "  function name:      $fn" >&2
  echo "$fn"
}

fn_for_string() {
  local binary="$1" search="$2"
  local vma
  vma=$(string_vma "$binary" "$search")
  fn_at_vma "$binary" "$vma"
}

echo "=== verify ==="
go version -m v1_plain 2>&1 | grep -q "garble_target" \
  || fail "v1_plain does not contain expected module info"
echo "v1_plain: symbols + module info intact"
go version -m v2_garbled 2>&1 | grep -q "unknown" \
  || fail "v2_garbled does not appear to be garbled"
echo "v2_garbled: module info stripped (garble confirmed)"
echo "=== v1 nm ==="
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
echo "=== string→fn mapping ==="
S1="connection refused: server not accepting connections"
echo "Locating '$S1':"
fn1_v1=$(fn_for_string v1_plain "$S1")
assert_eq "plain function for '$S1'" "$fn1_v1" "main.connectToServer"
fn1_v2=$(fn_for_string v2_garbled "$S1")
echo "  → $fn1_v2"
S2="failed to read config file"
echo "Locating '$S2':"
fn2_v1=$(fn_for_string v1_plain "$S2")
assert_eq "plain function for '$S2'" "$fn2_v1" "main.loadConfig"
fn2_v2=$(fn_for_string v2_garbled "$S2")
echo "  → $fn2_v2"
[ "$fn1_v2" != "$fn2_v2" ] \
  || fail "connectToServer and loadConfig garbled to same name ($fn1_v2)"
echo "PASS: distinct source functions → distinct garbled names"
echo "=== v2-only string ==="
S_NEW="invalid port: must be between 1 and 65535"
! grep -qaF "$S_NEW" v1_plain \
  || fail "'$S_NEW' found in v1_plain — expected only in v2"
echo "'$S_NEW': absent from v1 (validateConfig only exists in v2)"
echo "Locating '$S_NEW' in v2_garbled:"
fn_new=$(fn_for_string v2_garbled "$S_NEW")
echo "  → $fn_new"
[ "$fn_new" != "$fn1_v2" ] \
  || fail "validateConfig collides with connectToServer garbled name"
[ "$fn_new" != "$fn2_v2" ] \
  || fail "validateConfig collides with loadConfig garbled name"
echo "PASS: three distinct garbled names for three distinct source functions"
echo "=== summary ==="
echo "  connectToServer  →  $fn1_v2"
echo "  loadConfig       →  $fn2_v2"
echo "  validateConfig   →  $fn_new  (v2-only)"
echo "All assertions passed."
