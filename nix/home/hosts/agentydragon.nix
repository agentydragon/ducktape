# Agentydragon host-specific home-manager configuration
#
# To apply: home-manager switch --flake ~/code/ducktape#agentydragon --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}:
let
  tana = pkgs.callPackage ../packages/tana.nix { };
in
{
  imports = [
    ../home.nix
    ../modules/popos-bazel.nix
    # TODO: Fix cosmic.nix - the source path doesn't exist
    # ../modules/cosmic.nix
  ];

  # Agentydragon-specific configuration (desktop with full GUI)
  home.stateVersion = "24.05";

  home.packages = [ tana ];
  # TODO: Re-enable when google-drive-service module is fixed (see home.nix imports)
  # services.google-drive.enable = true;
}
