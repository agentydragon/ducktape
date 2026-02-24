#!/usr/bin/env bash
set -euo pipefail

# Build and push the rspcache image using Docker buildx with a remote cache.
# The script publishes to the primary registry while storing the build cache so
# subsequent builds can reuse layers.

this_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${this_dir}/../.." && pwd)"

TAG="${TAG:-latest}"
REGISTRY="${REGISTRY:-registry.k3s.agentydragon.com}"
CACHE_REF="${CACHE_REF:-${REGISTRY}/rspcache:cache}"
BUILDER_NAME="${BUILDER_NAME:-rspcache-buildx}"
PLATFORMS="${PLATFORMS:-linux/amd64}"

if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
  docker buildx create \
    --name "${BUILDER_NAME}" \
    --driver docker-container \
    --use \
    --bootstrap
fi

docker buildx use "${BUILDER_NAME}"

docker buildx build \
  --platform "${PLATFORMS}" \
  --cache-from "type=registry,ref=${CACHE_REF}" \
  --cache-to "type=registry,ref=${CACHE_REF},mode=max" \
  --tag "${REGISTRY}/rspcache:${TAG}" \
  -f "${repo_root}/docker/rspcache/Dockerfile" \
  "${repo_root}" \
  --push
