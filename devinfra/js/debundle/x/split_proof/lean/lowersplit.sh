#!/usr/bin/env bash
set -euo pipefail

core=$(rlocation "$1")
export SPLIT_PROOF_MODULE_IO
SPLIT_PROOF_MODULE_IO=$(rlocation "$2")
shift 2
exec "$core" "$@"
