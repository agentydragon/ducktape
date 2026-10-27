#!/bin/sh
set -eu

# Seed the Piraeus SSD claim from the static PV's IQ4 directory, then hold the copy
# to the same digests and lengths link-ssd-models.sh trusts. Reruns recopy.
# A plain cp OOMs the Job's memory cgroup: dirty page cache outruns writeback to the
# DRBD device. Copying in fsync'd chunks bounds the dirty pages to one chunk.
src=/ssd-models
dst=/ssd-models-new
chunk_mib=64
for manifest in /scripts/qwen38-ssd-shards.tsv /scripts/qwen38-ssd-derived-shards.tsv; do
  while read -r digest size relative_path; do
    mkdir -p "$(dirname "$dst/$relative_path")"
    : >"$dst/$relative_path"
    chunks=$(((size + chunk_mib * 1048576 - 1) / (chunk_mib * 1048576)))
    i=0
    while [ "$i" -lt "$chunks" ]; do
      dd if="$src/$relative_path" of="$dst/$relative_path" bs="${chunk_mib}M" count=1 \
        skip="$i" seek="$i" conv=notrunc,fsync 2>/dev/null
      i=$((i + 1))
    done
    test "$(stat -c %s "$dst/$relative_path")" = "$size"
    echo "$digest  $dst/$relative_path" | sha256sum -c -
  done <"$manifest"
done
