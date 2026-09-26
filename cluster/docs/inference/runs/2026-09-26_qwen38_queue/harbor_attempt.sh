#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ ${1:-} == --help ]]; then
  echo 'Usage: harbor_attempt.sh OUTPUT MODEL CONTEXT [--config-only|--install-only]'
  exit 0
fi
output=${1:?output directory}
model=${2:?model alias}
context=${3:?server context}
config_only=${4:-}
[[ -z $config_only || $config_only == --config-only || $config_only == --install-only ]]
install_only=false
if [[ $config_only == --install-only ]]; then install_only=true; fi
harbor=/home/agentydragon/.local/share/uv/tools/harbor/bin/harbor
harbor_package=/home/agentydragon/.local/share/uv/tools/harbor/lib/python3.12/site-packages/harbor
task=/home/agentydragon/.cache/harbor/tasks/packages/terminal-bench/interleaved-vigenere/238a75a32aad5e60b33a22ac53087790e95b752c6272f41d724407f198df4a14
input_limit=$((context - 32768))
output_limit=32768
threshold=32768
turns=500
printf '%s  %s\n' d2b9b4f98304d5458ccde463b9670a9ddd89cd4c01bbd03e93a4b92860bcab8a "$task/task.toml" | sha256sum --check
mkdir -p "$output"
"$harbor" --version >"$output/harbor-version.txt"
rg -q '0\.23\.0' "$output/harbor-version.txt"
sha256sum "$harbor_package/agents/terminus_2/terminus_2.py" "$harbor_package/llms/lite_llm.py" \
  "$harbor_package/models/job/config.py" >"$output/harbor-source-sha256.txt"
child=
cleanup() {
  if [[ -n $child ]] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
  fi
  # Only compose projects whose unique trial name is recorded by this invocation.
  shopt -s nullglob
  local config trial id project
  for config in "$output"/jobs/attempt/*/config.json; do
    trial=$(jq -r '.trial_name // empty | ascii_downcase' "$config")
    [[ $trial =~ ^[a-z0-9_-]+$ ]] || continue
    while read -r id; do
      [[ -n $id ]] || continue
      project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null) || continue
      if [[ $project == "${trial}__"* ]]; then
        docker logs "$id" >"$output/container-$id.log" 2>&1 || true
        docker rm -f "$id" >/dev/null || true
      fi
    done < <(docker ps -aq --filter label=com.docker.compose.project)
    while read -r id; do
      [[ -n $id ]] || continue
      project=$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' "$id" 2>/dev/null) || continue
      if [[ $project == "${trial}__"* ]]; then docker network rm "$id" >/dev/null || true; fi
    done < <(docker network ls -q --filter label=com.docker.compose.project)
  done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
jq -n --arg output "$output" --arg model "openai/$model" --arg task "$task" \
  --argjson input_limit "$input_limit" --argjson output_limit "$output_limit" \
  --argjson threshold "$threshold" --argjson turns "$turns" \
  --argjson install_only "$install_only" \
  '{job_name:"attempt",jobs_dir:($output+"/jobs"),n_attempts:1,n_concurrent_trials:1,
    install_only:$install_only,
    timeout_multiplier:1.0,retry:{max_retries:0},
    agents:[{name:"terminus-2",model_name:$model,
      kwargs:{api_base:"http://127.0.0.1:19080/v1",max_turns:$turns,temperature:0.6,
        enable_summarize:true,proactive_summarization_threshold:$threshold,
        record_terminal_session:false,store_all_messages:true,
        trajectory_config:{raw_content:false,linear_history:false},
        model_info:{max_input_tokens:$input_limit,max_output_tokens:$output_limit,
          input_cost_per_token:0,output_cost_per_token:0},
        llm_kwargs:{api_key:"not-needed",timeout:3600,max_tokens:$output_limit,num_retries:0},
        llm_call_kwargs:{extra_body:{chat_template_kwargs:{enable_thinking:true,reasoning_effort:"xhigh"}}}},
      env:{OPENAI_API_KEY:"not-needed"}}],
    tasks:[{path:$task}]}' >"$output/job.json"
if [[ $config_only == --config-only ]]; then exit 0; fi
# The original task allows 28,800 s. No outer timer silently shortens that deadline.
"$harbor" run --config "$output/job.json" >"$output/harbor.log" 2>&1 &
child=$!
wait "$child"
child=
