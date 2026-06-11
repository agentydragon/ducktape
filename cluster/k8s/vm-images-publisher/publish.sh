#!/usr/bin/env bash
# Build .#bootstrap-image at the requested git ref and upload the qcow2 to
# SeaweedFS via the in-cluster S3 endpoint. See README.md for context.
#
# Runtime deps (git, awscli2, gawk) are provided by the wrapping `nix shell`
# in cronjob.yaml. coreutils + findutils are already in the nixos/nix image.
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

: "${REF:=devel}"
: "${REPO:=https://github.com/agentydragon/ducktape}"
: "${FLAKE_BASE:=github:agentydragon/ducktape}"
: "${IMAGE_OUTPUT:=bootstrap-image}"
: "${OBJECT_PREFIX:=bootstrap}"
: "${S3_BUCKET:=vm-images}"
: "${S3_ENDPOINT:=http://public-s3.seaweedfs.svc.cluster.local:8333}"

GIT_SHA="$(git ls-remote "$REPO" "refs/heads/${REF}" | awk '{print $1}')"
if [ -z "${GIT_SHA}" ]; then
  echo "could not resolve ${REPO} refs/heads/${REF}" >&2
  exit 1
fi
echo "resolved ${REF} -> ${GIT_SHA}"

out_path="$(nix build --no-link --print-out-paths "${FLAKE_BASE}/${GIT_SHA}#${IMAGE_OUTPUT}" | tail -n1)"
image="$(find "$out_path" -maxdepth 1 -name '*.qcow2' -print -quit)"
if [ -z "$image" ]; then
  echo "no qcow2 under ${out_path}" >&2
  find "$out_path" -maxdepth 2 -type f >&2
  exit 1
fi

sha256="$(sha256sum "$image" | awk '{print $1}')"
size_bytes="$(stat -c '%s' "$image")"
echo "image ${image} sha256=${sha256} size=${size_bytes} B"

key="${OBJECT_PREFIX}/${GIT_SHA}.qcow2"
sha_key="${key}.sha256"
latest_key="${OBJECT_PREFIX}/${REF}.latest.txt"

aws --endpoint-url "$S3_ENDPOINT" s3 cp \
  "$image" "s3://${S3_BUCKET}/${key}" \
  --content-type application/octet-stream \
  --metadata "sha256=${sha256},git_sha=${GIT_SHA},ref=${REF},flake_output=${IMAGE_OUTPUT}"

printf '%s  %s\n' "$sha256" "$key" \
  | aws --endpoint-url "$S3_ENDPOINT" s3 cp - "s3://${S3_BUCKET}/${sha_key}" \
    --content-type text/plain

printf '%s\n' "$key" \
  | aws --endpoint-url "$S3_ENDPOINT" s3 cp - "s3://${S3_BUCKET}/${latest_key}" \
    --content-type text/plain

aws --endpoint-url "$S3_ENDPOINT" s3api head-object \
  --bucket "$S3_BUCKET" --key "$key" \
  --query '{ContentLength:ContentLength,Metadata:Metadata}'

echo "published s3://${S3_BUCKET}/${key}"
