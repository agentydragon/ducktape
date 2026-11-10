#!/usr/bin/env bash
set -euo pipefail

help="$("$(rlocation "$1")" --help)"
[[ "$help" == *"serve"* ]]
[[ "$help" == *"export"* ]]
