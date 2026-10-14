#!/usr/bin/env bash
set -euo pipefail

base=${1:?Usage: measure.sh OLLAMA_BASE MODEL OUTPUT_DIRECTORY}
model=${2:?Usage: measure.sh OLLAMA_BASE MODEL OUTPUT_DIRECTORY}
output=${3:?Usage: measure.sh OLLAMA_BASE MODEL OUTPUT_DIRECTORY}
mkdir -p "$output"

request() {
  curl --fail-with-body --silent --show-error --max-time 900 \
    "$base/api/chat" -H 'Content-Type: application/json' --data-binary "@$1" >"$2"
}

curl --fail --silent --show-error "$base/api/version" >"$output/version.json"
curl --fail --silent --show-error "$base/api/tags" >"$output/models.json"
jq -n --arg model "$model" '{model: $model}' \
  | curl --fail --silent --show-error "$base/api/show" -H 'Content-Type: application/json' \
    --data-binary @- >"$output/model-info.json"
jq -n --arg model "$model" '{model: $model, keep_alive: 0}' \
  | curl --fail --silent --show-error "$base/api/generate" -H 'Content-Type: application/json' \
    --data-binary @- >"$output/unload.json"
jq -n --arg model "$model" '{model: $model, stream: false, think: false,
  keep_alive: "10m", messages: [{role: "user", content: "Reply with OK."}],
  options: {num_ctx: 32768, num_predict: 4, temperature: 0, seed: 1}}' >"$output/load-request.json"
request "$output/load-request.json" "$output/load-response.json"

for words in 384 3072; do
  for repeat in 1 2 3; do
    prefix="$output/words-$words-repeat-$repeat"
    jq -n --arg model "$model" --argjson words "$words" --argjson repeat "$repeat" '
      ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
       "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi",
       "rho", "sigma", "tau", "upsilon"] as $vocabulary |
      ([range(0; $words) | $vocabulary[(. * 7 + $repeat * 13) % 20]] | join(" ")) as $text |
      {model: $model, stream: false, think: false, keep_alive: "10m",
       messages: [{role: "user", content: ("Run " + ($repeat | tostring) + ": " + $text +
         "\nWrite a well-commented Python module implementing stable topological sorting. Include tests and examples.")}],
       options: {num_ctx: 32768, num_predict: 256, temperature: 0, seed: $repeat}}' >"$prefix-request.json"
    cat /proc/meminfo >"$prefix-memory-before.txt"
    date --utc --iso-8601=seconds >"$prefix-started.txt"
    for supply in /sys/class/power_supply/*; do
      for attribute in online capacity status; do
        if [[ -f "$supply/$attribute" ]]; then
          printf '%s=%s\n' "$supply/$attribute" "$(cat "$supply/$attribute")"
        fi
      done
    done >"$prefix-power.txt"
    request "$prefix-request.json" "$prefix-response.json"
    cat /proc/meminfo >"$prefix-memory-after.txt"
    curl --fail --silent --show-error "$base/api/ps" >"$prefix-residency.json"
    jq '{prompt_eval_count, prompt_eval_duration, eval_count, eval_duration,
         total_duration, load_duration, done_reason,
         prefill_tokens_per_second: (.prompt_eval_count * 1e9 / .prompt_eval_duration),
         decode_tokens_per_second: (.eval_count * 1e9 / .eval_duration)}' \
      "$prefix-response.json" >"$prefix-metrics.json"
  done
done
jq -n --arg model "$model" '{model: $model, keep_alive: 0}' \
  | curl --fail --silent --show-error "$base/api/generate" -H 'Content-Type: application/json' \
    --data-binary @- >"$output/unload-after.json"
