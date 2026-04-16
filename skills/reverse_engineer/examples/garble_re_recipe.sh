#!/usr/bin/env bash
# Recipe: reverse engineering a garble-obfuscated Go binary.
#
# Shows what survives garble obfuscation and how to anchor known string
# literals to their garbled functions using standard Linux tools and
# go tool objdump (which reads pclntab — preserved by garble).
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

# fn_at_vma VMA → garbled function name from pclntab for the function that
# contains an instruction referencing VMA.
#
# go tool objdump reads pclntab (preserved by garble) and annotates each
# instruction with its containing function.  TEXT headers mark function
# boundaries; instructions that load string addresses reference the VMA.
# For stripped binaries without a symbol table, the disassembler shows the
# effective address as a bare hex value.
fn_at_vma() {
  local vma="$1"
  local hex="${vma#0x}" # strip 0x; match case-insensitively against output
  local result
  result=$(go tool objdump garbled-binary 2>/dev/null \
    | awk -v hex="$hex" '
        BEGIN { hex = tolower(hex) }
        /^TEXT / { fn = $2; sub(/\(.*/, "", fn) }
        fn && index(tolower($0), hex) { print fn; exit }
      ')
  if [ -z "$result" ]; then
    echo "FAIL: no function references VMA $vma" >&2
    exit 1
  fi
  echo "$result"
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
#   d. go tool objdump disassembles and shows pclntab function headers (TEXT)
#      and the effective address of memory references.  Tracking the last
#      TEXT header as we scan for the VMA gives us the containing function.

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
