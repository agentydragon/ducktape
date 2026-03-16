# Atlas host-specific home-manager configuration
# Proxmox VE host with desktop environment
#
# To apply: home-manager switch --flake ~/code/ducktape#atlas --impure
# (--impure needed for nixGL on non-NixOS systems)
{
  config,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    ../home.nix
  ];

  # Atlas-specific configuration (Proxmox host with GUI)
  home.stateVersion = "24.05";

  # Atlas runs on Proxmox VE (Debian-based), not NixOS
  # GUI is enabled in flake.nix, Kube is disabled
}
