# NixOS VM host-specific home-manager configuration (simplified)
{
  config,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    ../home.nix
    ../modules/no-screensaver.nix
  ];

  home.stateVersion = "24.05";
}
