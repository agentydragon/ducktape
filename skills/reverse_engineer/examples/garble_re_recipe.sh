#!/usr/bin/env bash
# Recipe: reverse engineering a garble-obfuscated Go binary.
#
# Shows what survives garble obfuscation and how to anchor known string
# literals to their garbled functions.
#
# Key insight: garble v0.13.0+ obfuscates the .gopclntab magic header bytes
# (first 4 bytes) to break go tool objdump, but the pclntab structure is
# intact. pclntool (examples/pclntool.go) patches the magic and uses
# debug/gosym to map any instruction PC to its garbled function name.
#
# Prerequisites:
#   - 'garbled-binary' in cwd
#   - 'objdump', 'readelf', 'strings' on PATH
#   - 'pclntool' on PATH  (build with: go build -o pclntool pclntool.go)

set -euo pipefail

# string_vma STRING → hex VMA of STRING's bytes in .rodata
string_vma() {
  local search="$1"
  local file_off sec_vma sec_file
  file_off=$(grep -obaF "$search" garbled-binary | head -1 | cut -d: -f1)
  [ -n "$file_off" ] || {
    echo "FAIL: '$search' not found in binary" >&2
    exit 1
  }
  sec_vma=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$5; exit}')
  sec_file=$(readelf -S garbled-binary | awk '/[ \t]\.rodata[ \t]/{print "0x"$6; exit}')
  printf "0x%x" $((sec_vma + file_off - sec_file))
}

# fn_at_vma VMA → garbled function name that loads the data at VMA.
#
# Two-step technique:
#   1. GNU objdump annotates RIP-relative memory references with their
#      effective address in a comment ("# 0xADDR"). Grep for our VMA.
#   2. pclntool patches the garbled .gopclntab magic and uses debug/gosym
#      to map the instruction PC to its containing function name.
fn_at_vma() {
  local vma="$1"
  local insn_line insn_addr

  # Step 1: find the instruction whose effective address comment matches VMA.
  insn_line=$(objdump -d garbled-binary 2>/dev/null | grep "# ${vma}$" | head -1)
  [ -n "$insn_line" ] || {
    echo "FAIL: no instruction references VMA $vma" >&2
    exit 1
  }
  insn_addr=0x$(echo "$insn_line" | awk '{print $1}' | tr -d ':')

  # Step 2: look up the instruction PC via patched pclntab.
  pclntool garbled-binary "$insn_addr"
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
