#!/usr/bin/env bash
# harbor_push.sh <bazel_load_target> <local_tag> <remote_repo>
#
# Runs `bazel run <bazel_load_target>` to build the OCI image and load it
# into the local Docker daemon, then pushes with tags: GITHUB_SHA,
# BRANCH-YYYYMMDDHHMMSS-sha7, and :latest (only when GITHUB_EVENT_NAME !=
# workflow_call).
# Skips entirely on pull_request builds.
#
# Environment variables (set automatically by GitHub Actions):
#   GITHUB_SHA          - full commit SHA
#   GITHUB_REF_NAME     - branch or tag name
#   GITHUB_EVENT_NAME   - event that triggered the workflow
set -euo pipefail

bazel_target="$1"
local_tag="$2"
remote_repo="$3"

if [[ "${GITHUB_EVENT_NAME:-}" == "pull_request" ]]; then
  echo "PR build — skipping push"
  exit 0
fi

bazel run "$bazel_target"

echo "$remote_repo: pushing"

BRANCH="${GITHUB_REF_NAME//\//-}"
TS="$(date -u +%Y%m%d%H%M%S)"
TAG="${BRANCH}-${TS}-${GITHUB_SHA:0:7}"
TAGS="$remote_repo:$GITHUB_SHA,$remote_repo:$TAG"
if [[ "${GITHUB_EVENT_NAME:-}" != "workflow_call" ]]; then
  TAGS="$TAGS,$remote_repo:latest"
fi

IFS=',' read -ra TAG_LIST <<<"$TAGS"
for tag in "${TAG_LIST[@]}"; do
  docker tag "$local_tag" "$tag"
  docker push --quiet "$tag"
done
