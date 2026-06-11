# Minimal NixOS VM home-manager configuration
#
# Used by terraform/nixos-dev-env/cloud-init.yaml.tpl for automated VM provisioning.
# Downloaded directly from GitHub during cloud-init to bootstrap VMs without
# requiring the full ducktape repo clone. Must remain standalone (no imports).
{
  config,
  pkgs,
  lib,
  ...
}: {
  home.username = "user";
  home.homeDirectory = "/home/user";
  home.stateVersion = "24.05";

  # Allow unfree packages
  nixpkgs.config.allowUnfree = true;

  # Basic packages
  home.packages = with pkgs; [
    htop
    vim
    curl
    wget
    git
  ];

  # Enable programs
  programs.bash.enable = true;
  programs.home-manager.enable = true;

  # GNOME/GUI settings (disable screensaver)
  dconf.settings = {
    "org/gnome/desktop/session" = {
      idle-delay = lib.hm.gvariant.mkUint32 0; # never
    };
    "org/gnome/desktop/screensaver" = {
      lock-enabled = false;
    };
  };
}
