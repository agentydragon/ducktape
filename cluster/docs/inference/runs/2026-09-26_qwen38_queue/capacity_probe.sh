#!/usr/bin/env bash
# Allocation/retrieval admission probe only, not a coding capability score.
set -euo pipefail
output=${1:?output directory}
model=${2:?model alias}
fill_tokens=${3:-120000}
base=http://127.0.0.1:19080
mkdir -p "$output"
post() {
  curl --fail --silent --show-error --max-time 3600 \
    --header 'Content-Type: application/json' --data-binary "@$2" "$base/$1" >"$3"
}
awk 'BEGIN {for (i=0;i<50000;i++) printf "Archive record %06d: component %d emitted event %d; status recorded for later inspection.\n", i, i%997, (i*17)%65521}' >"$output/filler.txt"
jq -Rs '{content:.,add_special:false}' "$output/filler.txt" >"$output/tokenize-request.json"
post tokenize "$output/tokenize-request.json" "$output/tokenize-response.json"
jq -e --argjson n "$fill_tokens" '.tokens | length >= $n' "$output/tokenize-response.json" >/dev/null
for part in 0 1 2 3; do
  jq --argjson n "$fill_tokens" --argjson part "$part" \
    '{tokens:.tokens[($part * ($n/4|floor)):(($part+1)*($n/4|floor))]}' \
    "$output/tokenize-response.json" >"$output/detokenize-$part-request.json"
  post detokenize "$output/detokenize-$part-request.json" "$output/detokenize-$part-response.json"
done
jq -n --arg model "$model" \
  --slurpfile a "$output/detokenize-0-response.json" \
  --slurpfile b "$output/detokenize-1-response.json" \
  --slurpfile c "$output/detokenize-2-response.json" \
  --slurpfile d "$output/detokenize-3-response.json" \
  '{model:$model,temperature:0,seed:42,max_tokens:256,cache_prompt:false,
    chat_template_kwargs:{enable_thinking:false},
    messages:[{role:"user",content:(
      "Read this archive and answer the retrieval question at the end.\n" +
      $a[0].content + "\nCANARY_ALPHA = marten-4819-copper\n" +
      $b[0].content + "\nCANARY_BETA = osprey-7263-lilac\n" +
      $c[0].content + "\nCANARY_GAMMA = badger-3907-silver\n" +
      $d[0].content + "\nReturn the exact values of CANARY_ALPHA, CANARY_BETA and CANARY_GAMMA as a JSON object. No other text."
    )}]}' >"$output/request.json"
post v1/chat/completions "$output/request.json" "$output/response.json"
jq -e '.choices[0].message.content |
  contains("marten-4819-copper") and contains("osprey-7263-lilac") and contains("badger-3907-silver")' \
  "$output/response.json" >"$output/retrieval-pass.json"
