#!/usr/bin/env bash
set -euo pipefail

# The manifest is trusted, declarative host configuration. llama.cpp owns HTTP,
# retries, and resumable temporary downloads; this adapter enforces our digests
# and skips the downloader entirely for offline cache hits.
manifest=$1
model_store=$2
ollama_blobs_dir=$3
install -d "$model_store/blobs" "$model_store/.staging"

verify_artifact() {
  local file=$1 digest=$2 size=$3
  [[ -f $file && ! -L $file ]] \
    && [[ $(stat --format=%s -- "$file") == "$size" ]] \
    && echo "$digest  $file" | sha256sum --check --status
}

artifacts=$(jq -er '.artifacts | to_entries[] | [.key, .value.sha256, (.value.size_bytes | tostring), .value.url] | @tsv' "$manifest")
while IFS=$'\t' read -r artifact_id digest size url; do
  echo "Checking $artifact_id ($size bytes)"
  destination="$model_store/blobs/sha256-$digest"
  if [[ -e $destination || -L $destination ]]; then
    # Fail closed, preserving corrupt cache objects for inspection.
    verify_artifact "$destination" "$digest" "$size"
    echo "Ready: $artifact_id (cached)"
    continue
  fi

  staged="$model_store/.staging/$digest.partial"
  if ! verify_artifact "$staged" "$digest" "$size"; then
    rm -f -- "$staged"
    old_blob="$ollama_blobs_dir/sha256-$digest"
    if verify_artifact "$old_blob" "$digest" "$size"; then
      ln -- "$old_blob" "$staged" 2>/dev/null || cp --reflink=auto --sparse=always -- "$old_blob" "$staged"
    else
      echo "Downloading $artifact_id ($size bytes)"
      llama download --model-url "$url" --model "$staged"
    fi
  fi
  if ! verify_artifact "$staged" "$digest" "$size"; then
    rm -f -- "$staged"
    echo "artifact failed pinned SHA-256/size verification: $artifact_id" >&2
    exit 1
  fi
  mv --no-target-directory -- "$staged" "$destination"
  echo "Ready: $artifact_id"
done <<<"$artifacts"
