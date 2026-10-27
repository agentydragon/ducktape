#!/bin/sh
set -eu

# Seed the Piraeus SSD claim from the static PV's IQ4 directory, then hold the copy
# to the same digests and lengths link-ssd-models.sh trusts. Reruns recopy.
src=/ssd-models
dst=/ssd-models-new
cp -a "$src/." "$dst/"
for manifest in /scripts/qwen38-ssd-shards.tsv /scripts/qwen38-ssd-derived-shards.tsv; do
  while read -r digest size relative_path; do
    test "$(stat -c %s "$dst/$relative_path")" = "$size"
    echo "$digest  $dst/$relative_path" | sha256sum -c -
  done <"$manifest"
done
sync
