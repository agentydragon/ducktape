#!/usr/bin/env bash
# Build/publish only the disposable spike image; never update a production pin.
set -euo pipefail
closure="$(nix eval --raw .#precommit.outPath)"
if [[ -n "${SPIKE_IMAGE:-}" ]]; then
  # Reuse the measured candidate while iterating on the harness. The container
  # job compares its embedded closure against this revision's evaluated closure.
  printf 'image=%s\nclosure=%s\nbuilt=false\n' "$SPIKE_IMAGE" "$closure" >>"$GITHUB_OUTPUT"
  exit 0
fi
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
nix path-info --closure-size "$closure"
# Explicit candidate tag; no latest tag, branch pin, or deploy is changed.
skopeo copy --insecure-policy "docker-archive:$work_dir/image" "docker://${IMAGE}:${GITHUB_SHA}"
digest="$(skopeo inspect --format '{{.Digest}}' "docker://${IMAGE}:${GITHUB_SHA}")"
printf 'image=%s@%s\nclosure=%s\nbuilt=true\n' "$IMAGE" "$digest" "$closure" >>"$GITHUB_OUTPUT"
