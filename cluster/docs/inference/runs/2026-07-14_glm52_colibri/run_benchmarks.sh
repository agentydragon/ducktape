#!/usr/bin/env bash
set -euo pipefail

RUN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECKOUT=${COLIBRI_CHECKOUT:-/var/lib/colibri/src/colibri}
MODEL=${COLIBRI_MODEL:-/var/lib/colibri/glm-5.2-colibri-int4-with-int8-mtp}
OUT=${COLIBRI_RESULTS:-$RUN_DIR/results/$(date +%Y%m%dT%H%M%S)}
PROMPT='Briefly explain why the sky appears blue.'
USAGE=$MODEL/.coli_usage

mkdir -p "$OUT"
"$RUN_DIR/verify_checkpoint.sh" "$MODEL"
[[ -x "$CHECKOUT/c/glm" ]] || {
  echo "run setup.sh first: missing $CHECKOUT/c/glm" >&2
  exit 1
}

had_usage=0
if [[ -f "$USAGE" ]]; then
  had_usage=1
  mv "$USAGE" "$OUT/coli_usage.before"
fi

restore_usage() {
  if [[ -f "$USAGE" ]]; then
    mv "$USAGE" "$OUT/coli_usage.after"
  fi
  if ((had_usage)); then
    cp "$OUT/coli_usage.before" "$USAGE"
  fi
}
trap restore_usage EXIT

run_one() {
  local name=$1
  local draft=$2
  local mtp=$3
  local stats=$4
  local pin=${5:-}
  local topp=${6:-0}
  local -a env_args=(
    "LD_LIBRARY_PATH=/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    "COLI_MODEL=$MODEL"
    DIRECT=1 PIPE_WORKERS=16 PREFETCH=1 CUDA_DENSE=1
    "DRAFT=$draft" "MTP=$mtp" "STATS=$stats"
  )
  local -a tier_args=()
  local -a sampling_args=()

  if [[ -n "$pin" ]]; then
    env_args+=("PIN=$pin" PIN_GB=99)
    tier_args=(--auto-tier --vram 52)
  fi
  if [[ "$topp" != 0 ]]; then
    sampling_args=(--topp "$topp")
  fi

  (
    cd "$CHECKOUT"
    env -u PYTHONPATH "${env_args[@]}" \
      nix develop "$CHECKOUT" --command python c/coli run \
      --model "$MODEL" --ram 64 --ctx 4096 --ngen 32 --temp 0 --gpu 0,1 \
      "${tier_args[@]}" "${sampling_args[@]}" "$PROMPT"
  ) 2>&1 | tee "$OUT/$name.log"
}

run_one cold 0 0 "$OUT/cold.stats"
run_one profiled 0 0 "$OUT/profiled.stats" "$OUT/cold.stats"
run_one refined-warm 0 0 "$OUT/refined-warm.stats" "$OUT/profiled.stats"
run_one mtp-depth-3 3 1 "$OUT/mtp-depth-3.stats" "$OUT/refined-warm.stats"
run_one expert-topp-0.7 0 0 "$OUT/expert-topp-0.7.stats" "$OUT/refined-warm.stats" 0.7

echo "benchmark artifacts written to $OUT"
