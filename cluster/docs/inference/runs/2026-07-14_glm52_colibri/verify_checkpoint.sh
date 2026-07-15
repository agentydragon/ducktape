#!/usr/bin/env bash
set -euo pipefail

MODEL=${1:-${COLIBRI_MODEL:-/var/lib/colibri/glm-5.2-colibri-int4-with-int8-mtp}}
EXPECTED_FILES=150
EXPECTED_SAFETENSORS=144
EXPECTED_BYTES=383760077466

[[ -d "$MODEL" ]] || {
  echo "missing model directory: $MODEL" >&2
  exit 1
}

read -r files safetensors bytes < <(
  find "$MODEL" -maxdepth 1 -type f \
    ! -name '.coli_usage' ! -name '*.stats' -printf '%f %s\n' \
    | awk '
      { files += 1; bytes += $2 }
      $1 ~ /\.safetensors$/ { safetensors += 1 }
      END { print files + 0, safetensors + 0, bytes + 0 }
    '
)
incomplete=$(find "$MODEL" -type f -name '*.incomplete' -printf . | wc -c)

[[ "$files" == "$EXPECTED_FILES" ]] || {
  echo "expected $EXPECTED_FILES root files, found $files" >&2
  exit 1
}
[[ "$safetensors" == "$EXPECTED_SAFETENSORS" ]] || {
  echo "expected $EXPECTED_SAFETENSORS safetensors, found $safetensors" >&2
  exit 1
}
[[ "$bytes" == "$EXPECTED_BYTES" ]] || {
  echo "expected $EXPECTED_BYTES bytes, found $bytes" >&2
  exit 1
}
[[ "$incomplete" == 0 ]] || {
  echo "found $incomplete incomplete files" >&2
  exit 1
}

for shard in 00000 00140; do
  find "$MODEL" -maxdepth 1 -type f -name "out-${shard}.safetensors" -print -quit \
    | grep -q . || {
    echo "missing main shard $shard" >&2
    exit 1
  }
done

mapfile -t mtp_sizes < <(
  find "$MODEL" -maxdepth 1 -type f -iname '*mtp*.safetensors' -printf '%s\n' | sort -n
)
expected_mtp=(1065950496 3527131672 5366238584)
[[ "${mtp_sizes[*]}" == "${expected_mtp[*]}" ]] || {
  echo "unexpected MTP shard sizes: ${mtp_sizes[*]:-none}" >&2
  exit 1
}

echo "checkpoint verified: $files files, $safetensors safetensors, $bytes bytes"
