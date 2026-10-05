#!/usr/bin/env bash
# Build/publish only the disposable spike image; never update a production pin.
set -euo pipefail
spike_dir="$(dirname "$0")"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
skopeo copy --insecure-policy --override-os linux --override-arch amd64 \
  "docker://$(cat "$spike_dir/base.txt")" \
  "docker-archive:$work_dir/base.tar:precommit-base:fixed"
export DUCKTAPE_PRECOMMIT_BASE_IMAGE
DUCKTAPE_PRECOMMIT_BASE_IMAGE="$(nix store add-file --name precommit-base.tar "$work_dir/base.tar")"
nix-store --add-root "$work_dir/base" --realise "$DUCKTAPE_PRECOMMIT_BASE_IMAGE"
nix build --impure --file "$spike_dir/image.nix" --out-link "$work_dir/image"
closure="$(nix eval --raw .#precommit.outPath)"
nix path-info --closure-size "$closure"
# Explicit candidate tag; no latest tag, branch pin, or deploy is changed.
skopeo copy --insecure-policy "docker-archive:$work_dir/image" "docker://${IMAGE}:${GITHUB_SHA}"
digest="$(skopeo inspect --format '{{.Digest}}' "docker://${IMAGE}:${GITHUB_SHA}")"
printf 'image=%s@%s\nclosure=%s\n' "$IMAGE" "$digest" "$closure" >>"$GITHUB_OUTPUT"
