#!/usr/bin/env bash
# Runs on the CI host after nix build; the container remains its non-root image user.
set -euo pipefail
loaded="$(docker load -i "$(readlink -f result)")"
printf '%s\n' "$loaded"
image="$(printf '%s\n' "$loaded" | sed -n -e 's/^Loaded image: //p' -e 's/^Loaded image ID: //p' | tail -n 1)"
test -n "$image"
docker run --rm --entrypoint /bin/claude "$image" --version
docker run --rm --entrypoint /bin/codex "$image" --version
nix build --no-link .#checks.x86_64-linux.runner-ducktape-tools
docker run --rm --network none --entrypoint /bin/bash "$image" -euo pipefail -c '
  test "$(id -u)" = 1000
  for tool in bb bbr bazelisk pre-commit ruff prettier nixfmt buildifier gazelle; do
    command -v "$tool"
  done
  bbr --help
  pre-commit --version
'
