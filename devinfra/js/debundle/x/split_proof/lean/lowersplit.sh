#!/usr/bin/env bash
set -euo pipefail

SPLIT_PROOF_MODULE_IO=$(rlocation '{MODULE_IO}')
export SPLIT_PROOF_MODULE_IO
exec "$(rlocation '{CORE}')" "$@"
