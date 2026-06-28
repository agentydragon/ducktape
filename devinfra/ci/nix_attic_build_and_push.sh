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

# NixOS configurations.
#
# Force services.google-drive off for any home-manager user that has it ENABLED
# (currently wyrm2, rugged) so the private gaffer `drivefs` closure never enters
# a closure we push to the broadly-readable `main` cache. Those hosts pull
# drivefs straight from the restricted `gaffer` cache at `nixos-rebuild switch`.
# See cluster/docs/nix_cache.md "Private-binary isolation (drivefs)".
#
# Detect by reading the enable bool per HM user — cheap, and it does not fetch
# drivefs (that lives behind `config = lib.mkIf cfg.enable`). Hosts where the
# option is absent (e.g. bazel-test's `root` user has home-manager but not the
# google-drive module) or false are built as-is: they can't leak drivefs, and
# injecting an undeclared option would error. Reading the bool this way avoids
# the infinite recursion an in-module options-existence guard would cause.
for host in $(nix eval --json .#nixosConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  override=
  for u in $(nix eval --impure --json ".#nixosConfigurations.$host.config.home-manager.users" \
    --apply builtins.attrNames 2>/dev/null | jq -r '.[]' 2>/dev/null); do
    if [ "$(nix eval --impure \
      ".#nixosConfigurations.$host.config.home-manager.users.$u.services.google-drive.enable" \
      2>/dev/null)" = true ]; then
      override=1
    fi
  done
  if [ -n "$override" ]; then
    target="(flake.nixosConfigurations.$host.extendModules {
      modules = [
        { home-manager.sharedModules = [ ({ lib, ... }: { services.google-drive.enable = lib.mkForce false; }) ]; }
      ];
    }).config.system.build.toplevel"
  else
    target="flake.nixosConfigurations.$host.config.system.build.toplevel"
  fi
  nix build --impure --expr "
    let flake = builtins.getFlake \"path:$(pwd)\";
    in $target
  " --no-link --print-out-paths >>"$out_paths"
done

# Home configurations — same rule: force google-drive off only where a config
# actually enables it (none today). The standalone claude-web profile doesn't
# import the module, so the option read errors and it is built as-is.
for host in $(nix eval --impure --json .#homeConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  if [ "$(nix eval --impure \
    ".#homeConfigurations.$host.config.services.google-drive.enable" 2>/dev/null)" = true ]; then
    target="(flake.homeConfigurations.$host.extendModules {
      modules = [ ({ lib, ... }: { services.google-drive.enable = lib.mkForce false; }) ];
    }).activationPackage"
  else
    target="flake.homeConfigurations.$host.activationPackage"
  fi
  nix build --impure --expr "
    let flake = builtins.getFlake \"path:$(pwd)\";
    in $target
  " --no-link --print-out-paths >>"$out_paths"
done

# Agent/bootstrap outputs. Keep the individual tools here even though devtools
# includes them: these are the bootstrap tools agent hosts need before they can
# run BuildBuddy-backed validation, so stale package composition should not hide
# a missing cache path.
for output in \
  .#packages.x86_64-linux.bb \
  .#packages.x86_64-linux.bbr \
  .#packages.x86_64-linux.bbapi \
  .#packages.x86_64-linux.devtools \
  .#devShells.x86_64-linux.default; do
  nix build --impure \
    "$output" \
    --no-link --print-out-paths >>"$out_paths"
done

echo "Final sweep: pushing $(wc -l <"$out_paths") paths to Attic (most already uploaded by watch-store)..."
xargs attic push ducktape:main <"$out_paths"
