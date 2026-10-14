#!/usr/bin/env bash
set -euo pipefail

# Build PR #10442's wrapper first; no manual LD_LIBRARY_PATH is needed.
litert_bin=${1:?Usage: reproduce.sh PATH_TO_LITERT_LM OUTPUT_DIRECTORY}
output=${2:?Usage: reproduce.sh PATH_TO_LITERT_LM OUTPUT_DIRECTORY}
mkdir -p "$output"
for prefill in 512 2048; do
  env -u LD_LIBRARY_PATH \
    VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json \
    /run/current-system/sw/bin/time -v "$litert_bin" benchmark gemma4-e2b-it \
    --backend gpu --prefill-tokens "$prefill" --decode-tokens 128 \
    --max-num-tokens 32000 --runs 3 --speculative-decoding true \
    >"$output/benchmark_$prefill.log" 2>&1
done

# For HTTP probes, run this server separately, then POST tool-request.json or
# followup-request.json with curl --data-binary @FILE. The model must be cached.
# "$litert_bin" serve --host 127.0.0.1 --port 9379 --enable-speculative-decoding=true
