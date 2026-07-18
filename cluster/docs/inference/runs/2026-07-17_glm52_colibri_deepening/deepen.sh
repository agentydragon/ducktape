#!/usr/bin/env bash
# GLM-5.2 Colibri deepening: warm-cache steady state (longer ngen) + 64K-context
# allocation effect, vs the 2026-07-14 cold 4K/0.28 tok/s baseline.
#
# Reuses the run_one env contract from ../2026-07-14_glm52_colibri/run_benchmarks.sh.
# Run on wyrm2 after that bundle's setup.sh has built c/glm.
set -euo pipefail

CHECKOUT=${COLIBRI_CHECKOUT:-/var/lib/colibri/src/colibri}
MODEL=${COLIBRI_MODEL:-/var/lib/colibri/glm-5.2-colibri-int4-with-int8-mtp}
OUT=${COLIBRI_RESULTS:-/var/lib/colibri/deepen-$(date +%Y%m%dT%H%M%S)}
PROMPT='Briefly explain why the sky appears blue.'
USAGE=$MODEL/.coli_usage

mkdir -p "$OUT"
[[ -x "$CHECKOUT/c/glm" ]] || {
  echo "missing $CHECKOUT/c/glm — run setup.sh" >&2
  exit 1
}

had_usage=0
[[ -f "$USAGE" ]] && {
  had_usage=1
  mv "$USAGE" "$OUT/coli_usage.before"
}
restore_usage() {
  [[ -f "$USAGE" ]] && mv "$USAGE" "$OUT/coli_usage.after"
  ((had_usage)) && cp "$OUT/coli_usage.before" "$USAGE"
}
trap restore_usage EXIT

run_one() {
  local name=$1 ctx=$2 ngen=$3 pin=${4:-}
  local -a env_args=(
    "LD_LIBRARY_PATH=/run/opengl-driver/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    "COLI_MODEL=$MODEL" DIRECT=1 PIPE_WORKERS=16 PREFETCH=1 CUDA_DENSE=1
    DRAFT=0 MTP=0 "STATS=$OUT/$name.stats"
  )
  local -a tier_args=()
  [[ -n "$pin" ]] && {
    env_args+=("PIN=$pin" PIN_GB=99)
    tier_args=(--auto-tier --vram 52)
  }
  (
    cd "$CHECKOUT"
    env -u PYTHONPATH "${env_args[@]}" \
      nix develop "$CHECKOUT" --command python c/coli run \
      --model "$MODEL" --ram 64 --ctx "$ctx" --ngen "$ngen" --temp 0 --gpu 0,1 \
      "${tier_args[@]}" "$PROMPT"
  ) 2>&1 | tee "$OUT/$name.log"
}

# Warm progression at 4K with a longer generation (steady-state decode, less
# first-token warmup skew than the baseline's ngen=32).
run_one cold-4k 4096 64
run_one profiled-4k 4096 64 "$OUT/cold-4k.stats"
run_one warm-4k 4096 64 "$OUT/profiled-4k.stats"

# Same warmed routing history, but a 64K context allocation: does the larger KV
# buffer squeeze the VRAM expert hot-tier and slow decode?
run_one warm-64k 65536 64 "$OUT/profiled-4k.stats"

echo "deepening artifacts in $OUT"
