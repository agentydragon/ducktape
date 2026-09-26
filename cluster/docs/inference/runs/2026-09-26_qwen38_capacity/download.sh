#!/usr/bin/env bash
# Public, pinned files; no credential is needed. Downloads and hash checks are serial.
set -euo pipefail
root=/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF
revision=38bb39ee97821de2c9009abb7e93950eec396e66
manifest=$(dirname -- "${BASH_SOURCE[0]}")/downloads.tsv
reserve=$((128 * 1024 * 1024 * 1024))
mountpoint -q /var/lib/llm-models-ssd
mkdir -p "$root"
while IFS=$'\t' read -r sha size relative; do
  target="$root/$relative"
  partial="$target.partial"
  mkdir -p "$(dirname -- "$target")"
  if [[ -L $target || -L $partial ]]; then
    echo "Refusing symlink: $relative" >&2
    exit 1
  fi
  if [[ -e $target ]]; then
    [[ $(stat -c %s "$target") == "$size" ]]
    printf '%s  %s\n' "$sha" "$target" | sha256sum --check
    continue
  fi
  have=0
  if [[ -e $partial ]]; then have=$(stat -c %s "$partial"); fi
  ((have <= size))
  free=$(df -B1 --output=avail "$root" | tail -n 1)
  if ((free < size - have + reserve)); then
    echo "Insufficient space for $relative plus 128 GiB reserve" >&2
    exit 1
  fi
  echo "$(date -Is) downloading $relative"
  curl --fail --silent --show-error --location --retry 6 --retry-delay 10 \
    --continue-at - --limit-rate 40M --output "$partial" \
    "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/$revision/$relative?download=true"
  [[ $(stat -c %s "$partial") == "$size" ]]
  printf '%s  %s\n' "$sha" "$partial" | sha256sum --check
  mv --no-clobber -- "$partial" "$target"
  echo "$(date -Is) verified $relative"
done <"$manifest"
echo "$(date -Is) all downloads verified"
