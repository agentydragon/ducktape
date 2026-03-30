#!/usr/bin/env bash
# BB Push GHCR Images step: authenticate, push OCI images, tag for Flux.
#
# Expects: GHCR_USERNAME, GHCR_TOKEN env vars.
set -euo pipefail

# Honor [skip ci] — BuildBuddy Workflows doesn't natively support it.
if git log -1 --format='%s' | grep -qF '[skip ci]'; then
  echo "Commit message contains [skip ci], skipping image push."
  exit 0
fi

# Validate required secrets.
missing=()
[[ -z "${GHCR_USERNAME:-}" ]] && missing+=(GHCR_USERNAME)
[[ -z "${GHCR_TOKEN:-}" ]] && missing+=(GHCR_TOKEN)
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: Missing required env vars: ${missing[*]}" >&2
  echo "Configure these as BuildBuddy Workflow secrets." >&2
  exit 1
fi

# Authenticate crane to GHCR.
# oci_push reads ~/.docker/config.json for credentials.
mkdir -p ~/.docker
AUTH=$(echo -n "${GHCR_USERNAME}:${GHCR_TOKEN}" | base64 -w0)
cat >~/.docker/config.json <<ENDJSON
{"auths":{"ghcr.io":{"auth":"${AUTH}"}}}
ENDJSON

# Push images sequentially. bazel run builds (hitting RBE cache
# from the CI action) then executes crane push with :latest.
# After each push, `bazel run @crane -- tag` adds the pinned tag
# so Flux ImagePolicy can track deployable versions.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
TS=$(date -u +%Y%m%d%H%M%S)
SHA=$(git rev-parse --short=7 HEAD)
PINNED_TAG="${BRANCH}-${TS}-${SHA}"

push_and_tag() {
  local target="$1" repo="$2"
  echo "Pushing $target"
  bazel run --config=rbe --remote_download_toplevel "$target"
  echo "Tagging ${repo}:latest -> ${repo}:${PINNED_TAG}"
  bazel run --config=rbe --remote_download_toplevel @crane -- tag "${repo}:latest" "${PINNED_TAG}"
}

push_and_tag //cluster/k8s/inventree/token-provisioner:push ghcr.io/agentydragon/token-provisioner
push_and_tag //props/backend:push ghcr.io/agentydragon/props-backend
push_and_tag //airlock:push ghcr.io/agentydragon/airlock
push_and_tag //airlock/auth_proxy:push ghcr.io/agentydragon/auth-proxy
push_and_tag //mcp_infra/exec:direct_push ghcr.io/agentydragon/exec-backend
push_and_tag //openclaw/exec:push ghcr.io/agentydragon/openclaw-exec
push_and_tag //homeassistant/proxy:push ghcr.io/agentydragon/homeassistant-proxy
push_and_tag //inventree_utils/rai_plugin:push ghcr.io/agentydragon/inventree
push_and_tag //tana/token_broker:push ghcr.io/agentydragon/tana-token-broker
push_and_tag //third_party/activitywatch:push ghcr.io/agentydragon/aw-server
