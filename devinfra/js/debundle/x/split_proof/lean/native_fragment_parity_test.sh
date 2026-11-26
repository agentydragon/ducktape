#!/usr/bin/env bash
set -euo pipefail
binary="$(rlocation "$1")"
reference="$(rlocation "$2")"
native_output="$(env -i PATH=/nonexistent "$binary")"
interpreted_output="$("$reference")"
[[ "$native_output" == "$interpreted_output" ]]
[[ "$native_output" == $'true\nfalse' ]]
