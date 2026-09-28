#!/bin/sh
set -eu

# Shards were SHA256-verified by the pinned inference download recipe. Check
# presence/size at each start; avoid re-reading 94 GB on every Pod restart.
# Refuse to replace any existing blob or link to a different target.
mkdir -p /models/blobs
for manifest in /scripts/qwen38-ssd-shards.tsv /scripts/qwen38-ssd-derived-shards.tsv; do
  while read -r digest size relative_path; do
    source_path="/ssd-models/$relative_path"
    blob_path="/models/blobs/sha256-$digest"
    test -f "$source_path"
    test "$(stat -c %s "$source_path")" = "$size"
    if test -L "$blob_path"; then
      test "$(readlink "$blob_path")" = "$source_path"
    elif test -e "$blob_path"; then
      echo "Refusing to replace existing blob: $blob_path" >&2
      exit 1
    else
      ln -s "$source_path" "$blob_path"
    fi
  done <"$manifest"
done
