# GPD host-specific home-manager configuration
#
# To apply: cd ~/code/ducktape/nix/home && home-manager switch --flake .#gpd --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}: {
  imports = [
    ../home.nix
  ];

  # GPD-specific configuration (laptop with full GUI)
  home.stateVersion = "24.05";
  # TODO: Re-enable when google-drive-service module is fixed (see home.nix imports)
  # services.google-drive.enable = true;
}
