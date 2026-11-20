#!/usr/bin/env bash
set -euo pipefail

repo=ghost-actual/Qwen3.8-Flash-Next-Abliterated-EXL3-2.50bpw
revision=135402464a8d351ca6ecb6969f2a6f4061681a24
model=qwen38-exl3-250-abliterated
root=${1:-/games/llm-models}
target="$root/$model"
staging="$root/.$model.partial"

exec 9>"$root/.$model.lock"
flock 9
if [[ -L "$target" || -L "$staging" ]]; then
  echo "Checkpoint directories must be local directories." >&2
  exit 1
fi

verify() {
  hf cache verify "$repo" --revision "$revision" --local-dir "$1" --fail-on-missing-files
}

if [[ -e "$target" ]]; then
  verify "$target"
else
  env -u HF_XET_HIGH_PERFORMANCE HF_XET_NUM_CONCURRENT_RANGE_GETS=2 \
    hf download "$repo" --revision "$revision" --local-dir "$staging" --max-workers 2
  verify "$staging"
  mv -T --no-copy --update=none-fail -- "$staging" "$target"
fi
