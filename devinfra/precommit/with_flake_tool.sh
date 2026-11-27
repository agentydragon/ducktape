#!/usr/bin/env bash
# Run a hook with a flake tool. Prebuilt agent environments can work offline.
set -euo pipefail

tool=$1
shift
installed=$(command -v "$tool" || true)
if [[ ${DUCKTAPE_PRECOMMIT_USE_FLAKE:-} != 1 && -n $installed && $(readlink -f -- "$installed") == /nix/store/* ]]; then
  exec "$@"
fi
exec nix shell --no-update-lock-file ".#precommit-$tool" --command "$@"
