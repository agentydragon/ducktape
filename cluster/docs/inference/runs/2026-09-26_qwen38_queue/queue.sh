#!/usr/bin/env bash
# Serial, bounded work after downloads; guarded for the shared wyrm2 desktop.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=${1:?absolute output directory}
[[ $output == /* ]]
mkdir -p "$output"
exec 9>"${XDG_RUNTIME_DIR:-/run/user/1001}/wyrm2-qwen38-serial-queue.lock"
flock -n 9
exec > >(tee -a "$output/queue.log") 2>&1
image=ghcr.io/ggml-org/llama.cpp@sha256:014f721265464f38ccb247c1338d07d852c4bae7509a4b4734d07a2bbadc765c
root=/var/lib/llm-models-ssd
server=wyrm2-qwen38-serial-queue
run_id=$(basename -- "$output")
phase_pid=
current_run=
model=
model_file=
ram_gib=0
admission_deadline=$(($(date +%s) + 24 * 3600))

note() { echo "$(date -Is) $*"; }
stop_phase() {
  if [[ -n $phase_pid ]] && kill -0 "$phase_pid" 2>/dev/null; then
    kill -TERM -- "-$phase_pid" 2>/dev/null || true
    for _ in {1..30}; do
      kill -0 "$phase_pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$phase_pid" 2>/dev/null; then kill -KILL -- "-$phase_pid" 2>/dev/null || true; fi
    wait "$phase_pid" 2>/dev/null || true
  fi
  phase_pid=
}
stop_server() {
  if [[ $(docker inspect -f '{{index .Config.Labels "ducktape.queue-id"}}' "$server" 2>/dev/null || true) == "$run_id" ]]; then
    if [[ -n $current_run ]]; then
      docker logs "$server" >"$current_run/server.log" 2>&1 || true
      docker inspect "$server" >"$current_run/server-inspect.json" || true
    fi
    docker stop --time 20 "$server" >/dev/null || true
    docker rm "$server" >/dev/null || true
  fi
}
cleanup() {
  stop_server
  stop_phase
}
trap cleanup EXIT
trap 'note "queue interrupted"; exit 130' INT TERM

mem_available_kib() { awk '/MemAvailable:/ {print $2}' /proc/meminfo; }
ollama_paused() {
  kubectl --request-timeout=10s -n ollama get deployment ollama -o json \
    | jq -e '.spec.replicas == 0 and ((.status.replicas // 0) == 0)' >/dev/null
}
wait_admission() {
  local required=$(((ram_gib + 8 + 16) * 1024 * 1024))
  # Reserve two original 4 GiB task/verifier containers plus 16 GiB desktop RAM.
  while true; do
    if (($(date +%s) >= admission_deadline)); then
      note "admission deadline reached; no inference launched for $model"
      return 1
    fi
    if ollama_paused && (($(mem_available_kib) >= required)); then
      mapfile -t free_gpu < <(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits)
      if ((${#free_gpu[@]} == 2)) && ((free_gpu[0] >= 28000 && free_gpu[1] >= 30000)); then
        return 0
      fi
    fi
    note "waiting for paused Ollama, idle GPUs and $((required / 1024 / 1024)) GiB host headroom for $model"
    sleep 30
  done
}
check_runtime_headroom() {
  (($(mem_available_kib) >= 16 * 1024 * 1024)) || return 1
  mapfile -t free_gpu < <(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits)
  ((${#free_gpu[@]} == 2)) || return 1
  ((free_gpu[0] >= 6144 && free_gpu[1] >= 1024)) || return 1
  ollama_paused
}
run_guarded() {
  setsid "$@" &
  phase_pid=$!
  local status=0
  while kill -0 "$phase_pid" 2>/dev/null; do
    if ! check_runtime_headroom; then
      note "resource/service guard stopped this attempt; not a model-quality failure"
      printf '%s\n' 'resource_or_service_guard' >"$current_run/termination.txt"
      stop_server
      stop_phase
      return 1
    fi
    sleep 15
  done
  wait "$phase_pid" || status=$?
  phase_pid=
  return "$status"
}
start_server() {
  local context=$1 kv=$2
  wait_admission
  [[ ! -e "$current_run/server-id.txt" ]]
  docker run --detach --name "$server" --label ducktape.experiment=qwen38-serial-queue --label "ducktape.queue-id=$run_id" \
    --device=nvidia.com/gpu=all \
    --env CUDA_VISIBLE_DEVICES=GPU-690929fc-97bf-9d39-d2f5-0322f1715b16,GPU-6154a49f-2cad-6b72-1e85-09b8006d08b5 \
    --user 1001:100 --cap-drop ALL --security-opt no-new-privileges \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --memory="${ram_gib}g" --memory-swap="${ram_gib}g" --cpus=6 \
    --mount "type=bind,source=$root,target=/models,readonly" \
    --publish 127.0.0.1:19080:8080 --publish 172.17.0.1:19080:8080 \
    --env CUDA_CACHE_PATH=/tmp/cuda-cache \
    "$image" --model "/models/$model_file" --alias "$model" \
    --host 0.0.0.0 --port 8080 --ctx-size "$context" --parallel 1 \
    --threads 6 --threads-batch 6 --split-mode layer --fit-target 8192,2048 \
    --flash-attn on --cache-type-k "$kv" --cache-type-v "$kv" \
    --load-mode mmap --lazy-mode on --no-repack --cache-ram 0 \
    --chat-template-kwargs '{"reasoning_effort":"xhigh","enable_thinking":true}' \
    --metrics --reasoning-format deepseek -lv 4 >"$current_run/server-id.txt"
  local deadline=$(($(date +%s) + 900))
  while ! curl --fail --silent --max-time 5 http://127.0.0.1:19080/health >"$current_run/health.json"; do
    check_runtime_headroom
    [[ $(docker inspect -f '{{.State.Running}}' "$server") == true ]]
    (($(date +%s) < deadline))
    sleep 10
  done
  docker logs "$server" >"$current_run/startup.log" 2>&1
  rg -q "n_slots = 1, n_ctx_slot = $context" "$current_run/startup.log"
  curl --fail --silent --max-time 15 http://127.0.0.1:19080/props >"$current_run/props.json"
  nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv >"$current_run/gpu-ready.csv"
  free -b >"$current_run/host-ready.txt"
}

note 'starting verified downloads before any inference'
bash "$here/../2026-09-26_qwen38_capacity/download.sh"
printf '%s\n' "$(date -Is)" >"$output/downloads-verified.txt"
# One real task first. Observe natural compaction before expanding experiments.
model=qwen3.8-flash-next-iq4xs
model_file=Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf
ram_gib=24
current_run="$output/iq4xs-128k-q8_0-terminus"
mkdir -p "$current_run"
note 'starting one real Terminal-Bench task at 128K with Terminus-2 summarization'
start_server 131072 q8_0
run_guarded bash "$here/harbor_attempt.sh" "$current_run/harbor" "$model" 131072
stop_server
note 'real-task attempt finished; inspect reward and compaction artifacts before expanding'
printf '%s\n' "$(date -Is)" >"$output/completed.txt"
