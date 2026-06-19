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
# Force services.google-drive off for each system's home-manager users so the
# private gaffer `drivefs` closure is never pulled into a closure we push to the
# broadly-readable `main` cache. Only the workstation hosts (wyrm2, rugged)
# enable it; they pull drivefs straight from the restricted `gaffer` cache at
# `nixos-rebuild switch` and rebuild only the cheap home-manager generation
# diff. Keeping drivefs out of `main` is the whole point of the separate gaffer
# cache — see cluster/docs/nix_cache.md "Private-binary isolation (drivefs)".
#
# Probe for home-manager rather than hardcoding a host list: hosts without it
# (e.g. bootstrap) have no such option and are built as-is. A guard *inside* the
# module — conditioning config on whether the option exists — is not possible:
# referencing `options` triggers infinite recursion in the module fixpoint.
for host in $(nix eval --json .#nixosConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  if nix eval --impure ".#nixosConfigurations.$host.config.home-manager.users" \
    --apply builtins.attrNames >/dev/null 2>&1; then
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

# Home configurations: disable google-drive via extendModules so the private
# gaffer-private binary is never fetched at eval time.
#
# claude-web is a minimal standalone profile that does NOT import the
# google-drive module, so injecting the option there fails with "The option
# services.google-drive does not exist". Skip the override for it. (A generic
# guard on whether the option exists is not possible: referencing `options`
# from a module's config triggers infinite recursion in the module fixpoint.)
for host in $(nix eval --impure --json .#homeConfigurations --apply builtins.attrNames | jq -r '.[]'); do
  if [ "$host" = claude-web ]; then
    target="flake.homeConfigurations.$host.activationPackage"
  else
    target="(flake.homeConfigurations.$host.extendModules {
      modules = [ { services.google-drive.enable = false; } ];
    }).activationPackage"
  fi
  nix build --impure --expr "
    let flake = builtins.getFlake \"path:$(pwd)\";
    in $target
  " --no-link --print-out-paths >>"$out_paths"
done

# Other outputs
nix build --impure \
  .#packages.x86_64-linux.devtools \
  --no-link --print-out-paths >>"$out_paths"

echo "Final sweep: pushing $(wc -l <"$out_paths") paths to Attic (most already uploaded by watch-store)..."
xargs attic push ducktape:main <"$out_paths"
