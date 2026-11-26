#!/usr/bin/env bash
set -euo pipefail
binary="$(rlocation "$1")"
actual="$(env -i PATH=/nonexistent "$binary")"
[[ "$actual" == $'true\nfalse' ]]
