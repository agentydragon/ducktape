#!/usr/bin/env bash
# The branch-only image preparation job may publish this public hook closure.
# Credentials are scoped to this step, never the image or measured trial jobs.
set -euo pipefail
cache_token="$(sops --decrypt --extract '["attic_token"]' secrets/ci/attic-main-writer.sops.yaml)"
printf '::add-mask::%s\n' "$cache_token"
attic login ducktape https://cache.allegedly.works "$cache_token"
attic push ducktape:public "$PRECOMMIT_CLOSURE"
