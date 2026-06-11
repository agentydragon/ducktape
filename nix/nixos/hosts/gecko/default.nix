# Gecko - headless CLI-only NixOS VM (Proxmox)
{
  pkgs,
  lib,
  username,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  sshKeys = with keys; [
    wyrm2
    atlas
    rugged
  ];
in
{
  imports = [
    ../../modules/vm-hardware.nix
  ];

  environment.systemPackages = with pkgs; [
    neovim
    tmux
    htop
    ripgrep
    tree
    pv
    strace
    lsof
    home-manager
  ];

  users.users.${username} = {
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = sshKeys;
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys;
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  users.motd = "Gecko - headless CLI VM\n";
}
