#!/usr/bin/env bash
set -euo pipefail

SPLIT_PROOF_MODULE_IO=$(rlocation '{MODULE_IO}')
export SPLIT_PROOF_MODULE_IO
if [[ ${1:-} == --diagnostic ]]; then
  shift
  exec "$(rlocation '{DIAGNOSTIC}')" "$@"
fi
if [[ ${SPLIT_PROOF_STRATEGY_CENSUS:-} == 1 ]]; then
  exec "$(rlocation '{STRATEGY_CENSUS}')" "$(rlocation '{CORE}')" "$@"
fi
exec "$(rlocation '{CORE}')" "$@"
