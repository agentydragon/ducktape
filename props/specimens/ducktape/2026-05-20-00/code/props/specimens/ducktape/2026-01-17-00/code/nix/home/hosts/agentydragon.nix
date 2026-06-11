# Agentydragon host-specific home-manager configuration
#
# To apply: cd ~/code/ducktape/nix/home && home-manager switch --flake .#agentydragon --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}: {
  imports = [
    ../home.nix
    ../modules/popos-bazel.nix
    # TODO: Fix cosmic.nix - the source path doesn't exist
    # ../modules/cosmic.nix
  ];

  # Agentydragon-specific configuration (desktop with full GUI)
  home.stateVersion = "24.05";
  # TODO: Re-enable when google-drive-service module is fixed (see home.nix imports)
  # services.google-drive.enable = true;
}
