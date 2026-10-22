#!/usr/bin/env bash
# Build and optionally publish the LiteLLM Crossplane package with the patch
# tracked beside this script. The adapter commit and upstream Terraform provider
# version are pinned so a rerun produces the same package inputs.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 OUTPUT_DIR [--push]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
ADAPTER_REV="251c977247b8bfbcd1f2cf9978068a6a55cb9651"
PATCH="${SCRIPT_DIR}/0002-crossplane-root-key-organization-id.patch"
PATCH_SHA="$(sha256sum "$PATCH" | cut -c1-12)"
PACKAGE_VERSION="v2.1.0-1.ducktape.${PATCH_SHA}"
PUBLISH_REGISTRY="git.allegedly.works/ducktape-ci"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ducktape-litellm-xpkg.XXXXXXXX")"
trap 'rm -rf -- "$BUILD_ROOT"' EXIT

mkdir -p "$OUTPUT_DIR"
git clone --quiet --filter=blob:none --no-checkout "$ADAPTER_REPO" "$BUILD_ROOT/provider"
git -C "$BUILD_ROOT/provider" checkout --quiet --detach "$ADAPTER_REV"
git -C "$BUILD_ROOT/provider" submodule update --init --recursive --quiet
install -m 0644 "$PATCH" "$BUILD_ROOT/provider/hack/patches/0002-crossplane-root-key-organization-id.patch"

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
(cd "$PROVIDER_SRC" && go test ./internal/provider -run '^TestReadKeyClearsOrganizationIDWhenAPIOmitsIt$' -count=1)
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
