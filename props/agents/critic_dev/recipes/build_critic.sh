#!/usr/bin/env bash
# build_critic.sh — Build a custom critic image from a modified main.py
#
# Usage: build_critic.sh <path-to-custom-main.py> [variant-name]
#
# This script layers a custom main.py onto the base critic image using crane,
# computes the digest locally, pushes the image, and prints the digest.
#
# The resulting digest can be passed to:
#   start_critic(definition_id="sha256:...")
#
# Prerequisites:
#   - crane CLI available in PATH
#   - PROPS_REGISTRY_URL or PROPS_BACKEND_URL set
#
# Environment:
#   PROPS_REGISTRY_URL        Registry host, e.g. "registry:5000".
#                              Derived from PROPS_BACKEND_URL if unset.
#   PROPS_BACKEND_URL         Fallback: strip scheme to derive registry host.
#   PROPS_CRITIC_BASE_DIGEST  (optional) Base image digest. Defaults to
#                              resolving critic:latest from the registry.
#
# Steps:
#   1. Resolve base critic image (by digest or latest tag)
#   2. Create a new OCI layer overlaying custom main.py at the runfiles path
#   3. Append the layer and push the image
#   4. Print the resulting digest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CUSTOM_MAIN="${1:?Usage: build_critic.sh <path-to-custom-main.py> [variant-name]}"
VARIANT="${2:-custom}"

# Resolve relative paths against the script's directory.
if [[ "${CUSTOM_MAIN}" != /* ]]; then
  CUSTOM_MAIN="${SCRIPT_DIR}/${CUSTOM_MAIN}"
fi

# Derive registry from PROPS_BACKEND_URL if PROPS_REGISTRY_URL is not set.
if [[ -z "${PROPS_REGISTRY_URL:-}" ]]; then
  PROPS_REGISTRY_URL="$(echo "${PROPS_BACKEND_URL:?Set PROPS_REGISTRY_URL or PROPS_BACKEND_URL}" | sed 's|https\?://||')"
fi
REGISTRY="${PROPS_REGISTRY_URL}"

# Default to the built-in critic:latest tag if no explicit digest is provided
if [[ -n "${PROPS_CRITIC_BASE_DIGEST:-}" ]]; then
  BASE_REF="${REGISTRY}/critic@${PROPS_CRITIC_BASE_DIGEST}"
  echo "Using explicit base digest: ${PROPS_CRITIC_BASE_DIGEST}" >&2
else
  BASE_REF="${REGISTRY}/critic:latest"
  echo "Using default base: critic:latest" >&2
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

MAIN_PY_PATH="props/agents/critic/main.py"
echo "Overlaying ${CUSTOM_MAIN} at ${MAIN_PY_PATH}" >&2

# 1. Create a tarball with the custom main.py at the correct runfiles path
mkdir -p "${WORK_DIR}/layer/${MAIN_PY_PATH%/*}"
cp "${CUSTOM_MAIN}" "${WORK_DIR}/layer/${MAIN_PY_PATH}"
tar -cf "${WORK_DIR}/layer.tar" -C "${WORK_DIR}/layer" .

# 2. Append the layer to the base image
# --insecure: the in-cluster registry (localhost or k8s service) uses plain
# HTTP behind the auth proxy.  This is safe because the registry is only
# reachable from the agent pod network.  Do NOT use --insecure against
# registries reachable from untrusted networks.
crane mutate "${BASE_REF}" \
  --append "${WORK_DIR}/layer.tar" \
  --tag "${REGISTRY}/critic:${VARIANT}" \
  --output "${WORK_DIR}/image.tar" \
  --insecure

# 3. Compute the digest from the local tarball
DIGEST="$(crane digest --tarball "${WORK_DIR}/image.tar")"

# 4. Push and print digest
crane push "${WORK_DIR}/image.tar" "${REGISTRY}/critic:${VARIANT}" --insecure

echo "${DIGEST}"
