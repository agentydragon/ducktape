#!/usr/bin/env bash
set -euo pipefail

RUN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODEL=${COLIBRI_MODEL:-/var/lib/colibri/glm-5.2-colibri-int4-with-int8-mtp}
REPO=mateogrgic/GLM-5.2-colibri-int4-with-int8-mtp
REVISION=3cc8db99b1b13fc79325d987ba3c1c430766b3b8

mkdir -p "$MODEL"
env -u HF_XET_HIGH_PERFORMANCE HF_XET_NUM_CONCURRENT_RANGE_GETS=8 \
  nix develop "$RUN_DIR" --command env -u PYTHONPATH \
  hf download "$REPO" \
  --revision "$REVISION" \
  --local-dir "$MODEL" \
  --max-workers 8

"$RUN_DIR/verify_checkpoint.sh" "$MODEL"
