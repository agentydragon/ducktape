#!/usr/bin/env bash
# Keep the workflow entrypoint stable; the Python driver owns process cleanup.
set -euo pipefail
exec python3 -m devinfra.ci.nix_attic_build_and_push "$@"
