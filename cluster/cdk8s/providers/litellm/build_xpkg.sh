#!/usr/bin/env bash
# Build and optionally publish the LiteLLM Crossplane package from a pinned
# adapter commit. The package tag includes that commit so distinct sources never
# overwrite the same registry version.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 OUTPUT_DIR [--push]" >&2
  exit 2
fi

OUTPUT_DIR="$1"
MODE="${2:-build}"
case "$MODE" in
  build | --push) ;;
  *)
    echo "expected --push or no mode, got: $MODE" >&2
    exit 2
    ;;
esac

ADAPTER_REPO="https://github.com/agentydragon/provider-litellm.git"
ADAPTER_BRANCH="codex/known-null-key-organization-id"
ADAPTER_REV="5cc2f4308916642f4342cfd61e6ffe1362570fb0"
PACKAGE_VERSION="v2.1.0-1.ducktape.${ADAPTER_REV:0:12}"
PUBLISH_REGISTRY="git.allegedly.works/ducktape-ci"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ducktape-litellm-xpkg.XXXXXXXX")"
trap 'rm -rf -- "$BUILD_ROOT"' EXIT

mkdir -p "$OUTPUT_DIR"
git clone \
  --quiet --filter=blob:none --no-checkout --branch "$ADAPTER_BRANCH" \
  "$ADAPTER_REPO" "$BUILD_ROOT/provider"
git -C "$BUILD_ROOT/provider" checkout --quiet --detach "$ADAPTER_REV"
git -C "$BUILD_ROOT/provider" submodule update --init --recursive --quiet

PROVIDER_SRC="$BUILD_ROOT/work/tf-provider-src"
PROVIDER_MIRROR="$BUILD_ROOT/work/tf-mirror"
MAKE_ARGS=(
  "VERSION=${PACKAGE_VERSION}"
  "HOSTARCH=amd64"
  "PLATFORMS=linux_amd64"
  "TERRAFORM_PROVIDER_SRC=${PROVIDER_SRC}"
  "TERRAFORM_PROVIDER_MIRROR=${PROVIDER_MIRROR}"
)

make -C "$BUILD_ROOT/provider" "${MAKE_ARGS[@]}" terraform-provider
(cd "$PROVIDER_SRC" && go test ./internal/provider -run '^(TestReadKeyClearsOrganizationIDWhenAPIOmitsIt|TestKeyWriteOnlyCrossplane(Create|Update)RetainsPlannedValueProtocol)$' -count=1)
# This adapter's xpkg.build target builds the runtime image but does not build
# the Crossplane provider binary that the image Makefile stages into its context.
make -C "$BUILD_ROOT/provider" "${MAKE_ARGS[@]}" go.build
make -C "$BUILD_ROOT/provider" "${MAKE_ARGS[@]}" xpkg.build.provider-litellm

PACKAGE_FILE="$BUILD_ROOT/provider/_output/xpkg/linux_amd64/provider-litellm-${PACKAGE_VERSION}.xpkg"
[[ -f "$PACKAGE_FILE" ]] || {
  echo "missing built package: $PACKAGE_FILE" >&2
  exit 1
}
install -m 0644 "$PACKAGE_FILE" "$OUTPUT_DIR/$(basename "$PACKAGE_FILE")"
echo "Built ${PUBLISH_REGISTRY}/provider-litellm:${PACKAGE_VERSION}"
echo "Package file: ${OUTPUT_DIR}/$(basename "$PACKAGE_FILE")"

if [[ "$MODE" == "--push" ]]; then
  make -C "$BUILD_ROOT/provider" "${MAKE_ARGS[@]}" "XPKG_REG_ORGS=${PUBLISH_REGISTRY}" xpkg.release.publish
fi
