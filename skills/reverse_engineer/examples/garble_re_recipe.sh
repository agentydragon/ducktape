#!/usr/bin/env bash
# Recipe: reverse engineering a garble-obfuscated Go binary.
#
# Pclntab deobfuscation:
#   garble v0.13.0+ XORs the .gopclntab magic header bytes to break redress,
#   GoReSym, and debug/gosym. pclntool patch repairs the magic in a copy of
#   the binary, unlocking those tools on the patched output.
#
# String-to-function mapping:
#   pclntool pc maps any instruction PC to its garbled function name via the
#   repaired pclntab, enabling string → VMA → objdump → function anchoring.
#
# Prerequisites:
#   - 'garbled-binary' in cwd
#   - 'objdump', 'readelf', 'strings' on PATH
#   - 'pclntool' on PATH  (build with: go build -o pclntool pclntool.go)
#   - 'redress' on PATH   (go install github.com/goretk/redress@latest)
#   - 'GoReSym' on PATH   (go install github.com/mandiant/GoReSym@latest)
#   - 'python3' on PATH   (used to parse GoReSym JSON output)

set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# string_vma STRING → hex VMA of STRING's bytes in .rodata
string_vma() {
  local search="$1"
  local file_off sec_vma sec_file
  file_off=$(grep -obaF "$search" garbled-binary | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || fail "'$search' not found in binary"
  sec_vma=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  printf "0x%x" $((sec_vma + file_off - sec_file))
}

# fn_at_vma VMA → garbled function name that loads the data at VMA.
#
# Two-step technique:
#   1. GNU objdump annotates RIP-relative memory references with their
#      effective address in a comment ("# 0xADDR"). Grep for our VMA.
#   2. pclntool pc maps the instruction PC to its containing function name
#      via the repaired pclntab.
fn_at_vma() {
  local vma="$1"
  local insn_line insn_addr

  insn_line=$(objdump -d garbled-binary 2>/dev/null | grep "# ${vma}$" | head -1)
  [ -n "$insn_line" ] || fail "no instruction references VMA $vma"
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')

  pclntool pc garbled-binary "$insn_addr"
}

echo "=== Deobfuscate pclntab and enable downstream tools ==="
# pclntool patch writes a copy with the correct magic — all 4 bytes, nothing else changed.
pclntool patch garbled-binary garbled-binary-deobf

# redress could not read the garbled binary (returned 0 packages). After patching it works.
redress_out=$(redress packages garbled-binary-deobf 2>&1)
echo "$redress_out"
echo "$redress_out" | grep -q "^main" \
  || fail "redress did not find 'main' package in deobfuscated binary"

# GoReSym similarly requires a parseable pclntab.
goresym_fns=$(GoReSym garbled-binary-deobf \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('UserFunctions') or []))")
[ "$goresym_fns" -gt 0 ] || fail "GoReSym found no functions in deobfuscated binary"

echo "=== JSON struct tags (always preserved by garble) ==="
# garble cannot encrypt struct tags because encoding/json reads them at runtime.
tags=$(strings garbled-binary | grep -E 'json:"[^"]+"')
echo "$tags"
echo "$tags" | grep -q 'json:"token,omitempty"' || fail 'json:"token,omitempty" missing'

echo "=== String-to-function mapping ==="
strings_=(
  "connection refused: server not accepting connections" # connectToServer
  "missing required field: host"                         # connectToServer
  "failed to read config file"                           # loadConfig
)
fns=()
for s in "${strings_[@]}"; do
  vma=$(string_vma "$s")
  fn=$(fn_at_vma "$vma")
  echo "  '$s' → $fn"
  fns+=("$fn")
done

# strings[0] and [1] are from connectToServer → same garbled function
[ "${fns[0]}" = "${fns[1]}" ] \
  || fail "expected same function for connectToServer strings (got ${fns[0]} vs ${fns[1]})"

# strings[2] is from loadConfig → different garbled function
[ "${fns[0]}" != "${fns[2]}" ] \
  || fail "expected different function for loadConfig string (both map to ${fns[0]})"

echo "All checks passed."
