# VPS host-specific home-manager configuration
#
# To apply: cd ~/code/ducktape/nix/home && home-manager switch --flake .#vps
# (no --impure needed, no GUI/nixGL on server)
#
# Note: enableGui=false, enableKube=false set in flake.nix
{
  config,
  pkgs,
  lib,
  ...
}: {
  imports = [
    ../home.nix
  ];

  # VPS-specific configuration (minimal GUI, server-focused)
  # Set appropriate state version for VPS
  home.stateVersion = "24.05";
}
