#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "usage: prepare_llama_router_models.sh OLLAMA_MODELS_DIR ROUTER_STATE_DIR" >&2
  exit 2
fi

ollama_models_dir=$1
router_state_dir=$2
router_models_dir="$router_state_dir/models"

resolve_layer() {
  local tag=$1 media_type=$2 model_name tag_name manifest digest blob
  model_name="${tag%%:*}"
  tag_name="${tag#*:}"
  manifest="$ollama_models_dir/manifests/registry.ollama.ai/library/$model_name/$tag_name"

  if [[ ! -r $manifest ]]; then
    echo "missing Ollama model manifest for $tag: $manifest; prepare this tag with Ollama before enabling llama-router" >&2
    return 1
  fi

  digest=$(
    jq --exit-status --raw-output --arg media_type "$media_type" \
      '[.layers[] | select(.mediaType == $media_type) | .digest] | if length == 1 then .[0] else error("expected exactly one layer for media type") end' \
      "$manifest"
  )
  if [[ ! $digest =~ ^sha256:[[:xdigit:]]{64}$ ]]; then
    echo "invalid Ollama layer digest for $tag ($media_type): $digest" >&2
    return 1
  fi

  blob="$ollama_models_dir/blobs/sha256-${digest#sha256:}"
  if [[ ! -r $blob ]]; then
    echo "missing cached Ollama layer for $tag ($media_type): $blob" >&2
    return 1
  fi
  if [[ $(head -c 4 "$blob") != GGUF ]]; then
    echo "cached Ollama layer is not a GGUF file for $tag ($media_type): $blob" >&2
    return 1
  fi
  printf '%s\n' "$blob"
}

resolve_optional_projector() {
  local tag=$1 model_name tag_name manifest digest blob
  model_name="${tag%%:*}"
  tag_name="${tag#*:}"
  manifest="$ollama_models_dir/manifests/registry.ollama.ai/library/$model_name/$tag_name"

  if [[ ! -r $manifest ]]; then
    echo "missing Ollama model manifest for $tag: $manifest; prepare this tag with Ollama before enabling llama-router" >&2
    return 1
  fi

  digest=$(
    jq --exit-status --raw-output \
      '[.layers[] | select(.mediaType == "application/vnd.ollama.image.projector") | .digest] | if length == 0 then "" elif length == 1 then .[0] else error("expected at most one projector layer") end' \
      "$manifest"
  )
  if [[ -z $digest ]]; then
    return 0
  fi
  if [[ ! $digest =~ ^sha256:[[:xdigit:]]{64}$ ]]; then
    echo "invalid Ollama projector digest for $tag: $digest" >&2
    return 1
  fi

  blob="$ollama_models_dir/blobs/sha256-${digest#sha256:}"
  if [[ ! -r $blob ]]; then
    echo "missing cached Ollama projector layer for $tag: $blob" >&2
    return 1
  fi
  if [[ $(head -c 4 "$blob") != GGUF ]]; then
    echo "cached Ollama projector layer is not a GGUF file for $tag: $blob" >&2
    return 1
  fi
  printf '%s\n' "$blob"
}

materialize_model() {
  local model_id=$1 tag=$2 model_blob projector_blob model_dir
  model_dir="$router_models_dir/$model_id"
  model_blob=$(resolve_layer "$tag" application/vnd.ollama.image.model)
  projector_blob=$(resolve_optional_projector "$tag")
  install -d "$model_dir"
  ln -sfn "$model_blob" "$model_dir/model.gguf"
  if [[ -n $projector_blob ]]; then
    ln -sfn "$projector_blob" "$model_dir/mmproj.gguf"
  else
    rm -f "$model_dir/mmproj.gguf"
  fi
}

install -d "$router_models_dir"
materialize_model rugged-qwen3-4b-instruct-2507-q4_K_M qwen3:4b-instruct-2507-q4_K_M
materialize_model rugged-gemma4-e2b-it-qat gemma4:e2b-it-qat
materialize_model rugged-gemma4-e2b-it-qat-long-128k-b128 gemma4:e2b-it-qat
materialize_model rugged-gemma4-e4b gemma4:e4b
materialize_model rugged-qwen2.5-coder-7b qwen2.5-coder:7b
materialize_model rugged-qwen3-8b qwen3:8b
materialize_model rugged-qwen35-0.8b-256k qwen3.5:0.8b
