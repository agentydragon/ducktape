# Gecko - headless CLI-only NixOS VM (KubeVirt)
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
    gecko
  ];
in
{
  imports = [
    ../../modules/vm-hardware.nix
    ../../modules/bazel
    ../../modules/system-inspection-sudo.nix
    ../../modules/attic-substituter.nix
  ];

  # Pull substituter for cache.allegedly.works/{main,gaffer}. Reader JWT is
  # auto-rotated by attic-jwt-rotation CronJob; the SOPS file is decryptable
  # by the gecko host key + agentydragon user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/gecko-attic.yaml;
  };

  # Passwordless sudo for read-only system inspection commands used by agents.
  ducktape.systemInspectionSudo.enable = true;

  environment.systemPackages = with pkgs; [
    neovim
    tmux
    htop
    btop
    ripgrep
    fd
    fzf
    jq
    yq
    tree
    pv
    strace
    lsof
    sops
    ssh-to-age
    home-manager
  ];

  users.users.${username} = {
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = sshKeys;
    extraGroups = [ "systemd-journal" ];
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys;
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  users.motd = "Gecko - headless KubeVirt agent VM\n";
}
