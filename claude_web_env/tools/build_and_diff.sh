#!/bin/bash
# Build the Claude Code web container and generate a diff report.
#
# Uses Docker (docker build --network=host). Docker data-root must be on tmpfs
# (configured at /mnt/bazel-tmpfs/docker via session hooks).
#
# Usage:
#   ./tools/build_and_diff.sh              # Full build + diff
#   ./tools/build_and_diff.sh --diff-only  # Skip build, just regenerate diff
#   ./tools/build_and_diff.sh --capture-binaries  # Capture proprietary binaries only
#
# Run from claude_web_env/ directory.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_NAME="claude-code-web-recreated"
CONTAINER_NAME="capture-tmp"

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $1"; }

# Parse arguments
DIFF_ONLY=false
CAPTURE_BINARIES=false
for arg in "$@"; do
  case $arg in
    --diff-only)
      DIFF_ONLY=true
      shift
      ;;
    --capture-binaries)
      CAPTURE_BINARIES=true
      shift
      ;;
    -h | --help)
      echo "Usage: $0 [--diff-only] [--capture-binaries]"
      echo ""
      echo "Options:"
      echo "  --diff-only        Skip build, just regenerate diff report"
      echo "  --capture-binaries Capture proprietary binaries from live container"
      echo "  -h, --help         Show this help"
      exit 0
      ;;
  esac
done

capture_proprietary_binaries() {
  log_info "Capturing proprietary binaries from live container..."
  gzip -c /usr/local/bin/environment-manager >reference/environment-manager.gz
  log_info "Captured environment-manager: $(sha256sum /usr/local/bin/environment-manager | cut -d' ' -f1)"
  gzip -c /process_api >reference/process_api.gz
  log_info "Captured process_api: $(sha256sum /process_api | cut -d' ' -f1)"
  log_info "Proprietary binaries captured to reference/"
}

build_image() {
  log_info "Building Dockerfile with Docker..."
  # NOTE: Docker hits the gVisor overlay layer limit at ~35 layers (4096-byte
  # kernel mount option string limit). The Dockerfile has ~60 instructions. If
  # the build fails with "mount source: overlay... invalid argument", the
  # Dockerfile needs layers consolidated (merge ENV/RUN instructions) or a
  # multi-stage squash approach. See PLAN.md for details.
  #
  # Pass proxy environment variables as build args. Docker excludes predefined
  # proxy ARG names (http_proxy, https_proxy, etc.) from the cache key, so
  # passing a session-specific JWT proxy URL doesn't break layer caching.
  # The gVisor sandbox has no direct internet access; all traffic must go
  # through the egress proxy in $https_proxy.
  docker build --network=host \
    --build-arg "http_proxy=${https_proxy:-}" \
    --build-arg "https_proxy=${https_proxy:-}" \
    --build-arg "HTTP_PROXY=${HTTPS_PROXY:-${https_proxy:-}}" \
    --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}" \
    --build-arg "no_proxy=${no_proxy:-}" \
    --build-arg "NO_PROXY=${NO_PROXY:-}" \
    -t "$IMAGE_NAME" .
  log_info "Build complete: $IMAGE_NAME"
}

capture_live_manifest() {
  log_info "Capturing live manifest..."
  bazel run //claude_web_env/tools:capture_manifest -- >live-manifest.ndjson
  log_info "Live manifest: $(wc -l <live-manifest.ndjson) entries"
}

capture_built_manifest() {
  log_info "Capturing built manifest..."
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
  docker create --name "$CONTAINER_NAME" "$IMAGE_NAME" /bin/true

  # Docker lacks podman's `podman mount` command. Export the container
  # filesystem as a tar archive and extract to a temp directory, then run
  # capture_manifest on the extracted tree.
  TMPDIR=$(mktemp -d /tmp/built-rootfs-XXXXXX)
  log_info "Extracting built image filesystem to $TMPDIR..."
  docker export "$CONTAINER_NAME" | tar -x --numeric-owner -C "$TMPDIR"
  docker rm "$CONTAINER_NAME"

  bazel run //claude_web_env/tools:capture_manifest -- "$TMPDIR" >built-manifest.ndjson
  rm -rf "$TMPDIR"
  log_info "Built manifest: $(wc -l <built-manifest.ndjson) entries"
}

generate_diff_report() {
  log_info "Generating diff report..."
  bazel run //claude_web_env/tools:diff_manifests -- \
    live-manifest.ndjson built-manifest.ndjson \
    --exclusions exclusions.yaml -o diff_report.md
  log_info "Diff report written to diff_report.md"
  head -50 diff_report.md | grep -E "^(#|##|\*\*|[0-9]+\.)" | head -20
}

main() {
  if [[ "$CAPTURE_BINARIES" == "true" ]]; then
    capture_proprietary_binaries
    exit 0
  fi

  if [[ "$DIFF_ONLY" == "false" ]]; then
    capture_proprietary_binaries
    build_image
  else
    log_info "Skipping build (--diff-only)"
    if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
      log_error "Image $IMAGE_NAME not found. Run without --diff-only first."
      exit 1
    fi
  fi

  log_info "Capturing version snapshot..."
  VERSIONS_FILE="reference/versions-$(date -u +%Y-%m-%d).yaml"
  bazel run //claude_web_env/tools:capture_versions -- >"$VERSIONS_FILE"
  log_info "Version snapshot saved to $VERSIONS_FILE"

  capture_live_manifest
  capture_built_manifest
  generate_diff_report

  log_info "Done! Review diff_report.md and commit if changes are expected."
}

main "$@"
