#!/bin/bash
# Build the Claude Code web container and generate a diff report.
#
# Assumes session hooks have already run (podman available, tmpfs mounted
# at /mnt/bazel-tmpfs with VFS storage conf at /tmp/storage-tmpfs-vfs.conf).
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
STORAGE_CONF="/tmp/storage-tmpfs-vfs.conf"

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
  log_info "Building Dockerfile (this takes ~20 min on tmpfs)..."
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" \
    podman build --layers=false \
    --network=host --isolation=oci --format=docker \
    -t "$IMAGE_NAME" .
  log_info "Build complete: $IMAGE_NAME"
}

capture_live_manifest() {
  log_info "Capturing live manifest..."
  uv run --script tools/capture_manifest.py >live-manifest.ndjson
  log_info "Live manifest: $(wc -l <live-manifest.ndjson) entries"
}

capture_built_manifest() {
  log_info "Capturing built manifest..."
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman rm -f "$CONTAINER_NAME" 2>/dev/null || true
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" \
    podman create --name "$CONTAINER_NAME" "localhost/$IMAGE_NAME" /bin/true
  MOUNT_PATH=$(CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman mount "$CONTAINER_NAME")
  log_info "Mounted at: $MOUNT_PATH"
  uv run --script tools/capture_manifest.py "$MOUNT_PATH" >built-manifest.ndjson
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman unmount "$CONTAINER_NAME"
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman rm "$CONTAINER_NAME"
  log_info "Built manifest: $(wc -l <built-manifest.ndjson) entries"
}

generate_diff_report() {
  log_info "Generating diff report..."
  uv run --script tools/diff_manifests.py \
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

  if [[ ! -f "$STORAGE_CONF" ]]; then
    log_error "Storage config not found at $STORAGE_CONF. Session hooks must run first."
    exit 1
  fi

  if [[ "$DIFF_ONLY" == "false" ]]; then
    capture_proprietary_binaries
    build_image
  else
    log_info "Skipping build (--diff-only)"
    if ! CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman image exists "localhost/$IMAGE_NAME"; then
      log_error "Image $IMAGE_NAME not found. Run without --diff-only first."
      exit 1
    fi
  fi

  log_info "Capturing version snapshot..."
  VERSIONS_FILE="reference/versions-$(date -u +%Y-%m-%d).yaml"
  uv run --script tools/capture_versions.py >"$VERSIONS_FILE"
  log_info "Version snapshot saved to $VERSIONS_FILE"

  capture_live_manifest
  capture_built_manifest
  generate_diff_report

  log_info "Done! Review diff_report.md and commit if changes are expected."
}

main "$@"
