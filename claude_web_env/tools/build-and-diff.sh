#!/bin/bash
# Build the Claude Code web container and generate a diff report.
#
# This script automates the full build + diff workflow:
#   1. Set up tmpfs storage (if not already mounted)
#   2. Build the Dockerfile with VFS storage
#   3. Capture manifests from live and built images
#   4. Generate diff-report.md
#
# Usage:
#   ./tools/build-and-diff.sh              # Full build + diff
#   ./tools/build-and-diff.sh --diff-only  # Skip build, just regenerate diff
#   ./tools/build-and-diff.sh --capture-binaries  # Capture proprietary binaries only
#
# Requirements:
#   - podman available
#   - uv available (for running Python scripts)
#   - Run from claude_web_env/ directory

set -euo pipefail

cd "$(dirname "$0")/.."

# Configuration
TMPFS_PATH="/tmp/tmpfs-exec"
STORAGE_CONF="/tmp/storage-tmpfs-vfs.conf"
IMAGE_NAME="claude-code-web-recreated"
CONTAINER_NAME="capture-tmp"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

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

# Capture proprietary binaries from live container
capture_proprietary_binaries() {
  log_info "Capturing proprietary binaries from live container..."

  # environment-manager - Claude Code's environment manager
  if [[ -f /usr/local/bin/environment-manager ]]; then
    gzip -c /usr/local/bin/environment-manager >reference/environment-manager.gz
    log_info "Captured environment-manager: $(sha256sum /usr/local/bin/environment-manager | cut -d' ' -f1)"
  else
    log_warn "environment-manager not found at /usr/local/bin/environment-manager"
  fi

  # process_api - Anthropic's process API server
  if [[ -f /process_api ]]; then
    gzip -c /process_api >reference/process_api.gz
    log_info "Captured process_api: $(sha256sum /process_api | cut -d' ' -f1)"
  else
    log_warn "process_api not found at /process_api"
  fi

  log_info "Proprietary binaries captured to reference/"
}

# Setup tmpfs storage if needed
setup_storage() {
  if ! mountpoint -q "$TMPFS_PATH" 2>/dev/null; then
    log_info "Setting up tmpfs storage at $TMPFS_PATH..."
    mount -t tmpfs -o size=200G,exec tmpfs "$TMPFS_PATH"
  fi
  mkdir -p "$TMPFS_PATH/containers/"{storage,run}

  if [[ ! -f "$STORAGE_CONF" ]]; then
    log_info "Creating storage config at $STORAGE_CONF..."
    cat >"$STORAGE_CONF" <<'EOF'
[storage]
driver = "vfs"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF
  fi
}

# Build the Dockerfile
build_image() {
  log_info "Building Dockerfile (this takes ~20 min on tmpfs)..."
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" \
    podman build --layers=false \
    --network=host --isolation=oci --format=docker \
    -t "$IMAGE_NAME" .
  log_info "Build complete: $IMAGE_NAME"
}

# Capture live manifest
capture_live_manifest() {
  log_info "Capturing live manifest..."
  uv run --script tools/capture_manifest.py >live-manifest.ndjson
  log_info "Live manifest: $(wc -l <live-manifest.ndjson) entries"
}

# Capture built manifest
capture_built_manifest() {
  log_info "Capturing built manifest..."

  # Clean up any existing capture container
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman rm -f "$CONTAINER_NAME" 2>/dev/null || true

  # Create container and mount
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" \
    podman create --name "$CONTAINER_NAME" "localhost/$IMAGE_NAME" /bin/true

  MOUNT_PATH=$(CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman mount "$CONTAINER_NAME")
  log_info "Mounted at: $MOUNT_PATH"

  uv run --script tools/capture_manifest.py "$MOUNT_PATH" >built-manifest.ndjson

  # Cleanup
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman unmount "$CONTAINER_NAME"
  CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman rm "$CONTAINER_NAME"

  log_info "Built manifest: $(wc -l <built-manifest.ndjson) entries"
}

# Generate diff report
generate_diff_report() {
  log_info "Generating diff report..."
  uv run --script tools/diff_manifests.py \
    live-manifest.ndjson built-manifest.ndjson \
    --exclusions exclusions.yaml -o diff-report.md

  # Show summary
  echo ""
  log_info "Diff report written to diff-report.md"
  echo ""
  echo "=== Summary ==="
  head -50 diff-report.md | grep -E "^(#|##|\*\*|[0-9]+\.)" | head -20
}

# Main
main() {
  # Handle --capture-binaries only mode
  if [[ "$CAPTURE_BINARIES" == "true" ]]; then
    capture_proprietary_binaries
    exit 0
  fi

  setup_storage

  if [[ "$DIFF_ONLY" == "false" ]]; then
    # Capture proprietary binaries before build to ensure we have latest
    capture_proprietary_binaries
    build_image
  else
    log_info "Skipping build (--diff-only)"
    # Check image exists
    if ! CONTAINERS_STORAGE_CONF="$STORAGE_CONF" podman image exists "localhost/$IMAGE_NAME"; then
      log_error "Image $IMAGE_NAME not found. Run without --diff-only first."
      exit 1
    fi
  fi

  capture_live_manifest
  capture_built_manifest
  generate_diff_report

  log_info "Done! Review diff-report.md and commit if changes are expected."
}

main "$@"
