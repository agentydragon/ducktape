#!/usr/bin/env bash
# Build all Nix flake outputs and push closures to Attic binary cache.
set -euo pipefail

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

echo "Pushing $(wc -l <"$out_paths") paths to Attic..."
xargs attic push main <"$out_paths"
