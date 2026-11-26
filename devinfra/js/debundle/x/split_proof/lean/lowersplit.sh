#!/usr/bin/env bash
set -euo pipefail

SPLIT_PROOF_MODULE_IO=$(rlocation '{MODULE_IO}')
export SPLIT_PROOF_MODULE_IO
if [[ ${1:-} == --diagnostic ]]; then
  shift
  exec "$(rlocation '{DIAGNOSTIC}')" "$@"
fi
exec "$(rlocation '{CORE}')" "$@"
