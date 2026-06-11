#!/usr/bin/env bash
# Build all Nix flake outputs and push closures to Attic binary cache.
#
# `attic watch-store` runs in the background and uploads paths as they
# land in the local nix store, so partial progress is preserved even if
# a later build fails or the runner dies. The explicit `attic push` at
# the end is a safety net: attic dedupes by NAR hash, so paths the
# watcher already uploaded are a cheap no-op, and anything the watcher
# missed (e.g. paths from the final build that didn't drain in time)
# gets caught.
#
# TODO: drop the safety-net push once we know `attic watch-store` drains
# cleanly on SIGTERM. Today the CLI has no `--drain`/`--once` mode, so
# we don't trust the watcher alone.
set -euo pipefail

attic watch-store ducktape:main &
watcher_pid=$!
trap 'kill -TERM "$watcher_pid" 2>/dev/null || true; wait "$watcher_pid" 2>/dev/null || true' EXIT

out_paths=$(mktemp)

# NixOS configurations
for host in $(nix eval --json .#nixosConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  nix build --impure \
    ".#nixosConfigurations.$host.config.system.build.toplevel" \
    --no-link --print-out-paths >>"$out_paths"
done

# Home configurations (google-drive disabled via extendModules so the private
# gaffer-private binary is never fetched at eval time)
for host in $(nix eval --impure --json .#homeConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  nix build --impure --expr "
    let flake = builtins.getFlake \"path:$(pwd)\";
    in (flake.homeConfigurations.$host.extendModules {
      modules = [{ services.google-drive.enable = false; }];
    }).activationPackage
  " --no-link --print-out-paths >>"$out_paths"
done

# Other outputs
nix build --impure \
  .#packages.x86_64-linux.devtools \
  --no-link --print-out-paths >>"$out_paths"

echo "Final sweep: pushing $(wc -l <"$out_paths") paths to Attic (most already uploaded by watch-store)..."
xargs attic push ducktape:main <"$out_paths"
