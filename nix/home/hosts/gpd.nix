# GPD host-specific home-manager configuration
#
# To apply: home-manager switch --flake ~/code/ducktape#gpd --impure
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
  ];

  # GPD-specific configuration (laptop with full GUI)
  home.stateVersion = "24.05";

  home.packages = [ tana ];
  # TODO: Re-enable when google-drive-service module is fixed (see home.nix imports)
  # services.google-drive.enable = true;
}
