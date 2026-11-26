#!/usr/bin/env bash
set -euo pipefail

core=$(rlocation '{CORE}')
export SPLIT_PROOF_MODULE_IO
SPLIT_PROOF_MODULE_IO=$(rlocation '{MODULE_IO}')
exec "$core" "$@"
