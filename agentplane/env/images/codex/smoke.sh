#!/usr/bin/env bash
# The existing Nix image gate supplies result as this image's Docker archive.
set -euo pipefail
loaded="$(docker load -i "$(readlink -f result)")"
image="$(printf '%s\n' "$loaded" | sed -n -e 's/^Loaded image: //p' -e 's/^Loaded image ID: //p' | tail -n 1)"
test -n "$image"
test "$(docker run --rm --network none --user 107:107 "$image" --version)" = 'codex-cli 0.162.1'
# Prove the native bootstrap patch is present and required admission fails closed.
status=0
docker run --rm --network none --user 107:107 \
  -e CODEX_COMMAND_CGROUP_SOCKET=/missing-owner.sock "$image" --version || status=$?
test "$status" -eq 127
