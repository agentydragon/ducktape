#!/usr/bin/env bash
# Recipe: reverse engineering a garble-obfuscated Go binary.
#
# Shows what survives garble obfuscation and how to anchor known string
# literals to their garbled functions using standard Linux tools and
# go tool addr2line (which reads pclntab — preserved by garble).
#
# Expects: 'garbled-binary' in cwd, 'go' on PATH.

set -euo pipefail

# string_vma STRING → hex VMA of STRING's bytes in .rodata
string_vma() {
  local search="$1"
  local file_off sec_vma sec_file
  file_off=$(grep -oba "$search" garbled-binary | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || {
    echo "FAIL: '$search' not found in binary" >&2
    exit 1
  }
  sec_vma=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  printf "0x%x" $((sec_vma + file_off - sec_file))
}

# fn_at_vma VMA → garbled function name from pclntab for the instruction that loads VMA
fn_at_vma() {
  local vma="$1" insn_line insn_addr
  insn_line=$(objdump -d garbled-binary 2>/dev/null | grep "# ${vma}$" | head -1)
  [ -n "$insn_line" ] || {
    echo "FAIL: no instruction references VMA $vma" >&2
    exit 1
  }
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')
  echo "$insn_addr" | go tool addr2line garbled-binary 2>/dev/null | head -1
}

echo "=== 1. JSON struct tags (always preserved by garble) ==="
# garble cannot encrypt struct tags because encoding/json reads them at runtime.
tags=$(strings garbled-binary | grep -E 'json:"[^"]+"')
echo "$tags"
echo "$tags" | grep -q 'json:"token,omitempty"' || {
  echo 'FAIL: json:"token,omitempty" missing'
  exit 1
}
echo "PASS: struct tags survive garble"

echo ""
echo "=== 2. String-to-function mapping ==="
# Technique:
#   a. grep -oba finds the exact byte offset of the string in the file.
#   b. readelf -S gives .rodata section VMA and file offset.
#   c. Shell arithmetic: string_vma = rodata_vma + (file_off - rodata_file_off)
#   d. objdump -d disassembles; LEA instructions reference data VMAs in comments.
#   e. go tool addr2line reads pclntab (preserved by garble) to map the
#      instruction address to the garbled function name it belongs to.

strings_=(
  "connection refused: server not accepting connections" # connectToServer
  "missing required field: host"                         # connectToServer
  "failed to read config file"                           # loadConfig
)
fns=()
for s in "${strings_[@]}"; do
  vma=$(string_vma "$s")
  fn=$(fn_at_vma "$vma")
  echo "  '$s'"
  echo "    → $fn"
  fns+=("$fn")
done

# strings[0] and [1] are from connectToServer → same garbled function
[ "${fns[0]}" = "${fns[1]}" ] \
  || {
    echo "FAIL: expected same function for connectToServer strings (got ${fns[0]} vs ${fns[1]})"
    exit 1
  }
echo "PASS: connectToServer strings map to the same garbled function (${fns[0]})"

# strings[2] is from loadConfig → different garbled function
[ "${fns[0]}" != "${fns[2]}" ] \
  || {
    echo "FAIL: expected different function for loadConfig string (both map to ${fns[0]})"
    exit 1
  }
echo "PASS: loadConfig string maps to a different garbled function (${fns[2]})"
