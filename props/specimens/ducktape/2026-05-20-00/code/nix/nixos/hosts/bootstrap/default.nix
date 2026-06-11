# Generic bootstrap NixOS — minimal config for initial provisioning.
# Boots on VMs (and physical machines with enableAllFirmware).
# After first boot, deploy the real host config:
#   nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#<host>
{
  pkgs,
  lib,
  username,
  ...
}:
let
  sshKeys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFfzLZ7zOOMviYrrxeh1nSXdwu9uveSXr07EJI5NwFau agentydragon@wyrm2"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBoGigbjJZfs1+M6yUCJSBzUlu2mFcakFTmuxrN425fO agentydragon@atlas"
  ];
in
{
  hardware.enableAllFirmware = true;

  users.users.${username} = {
    shell = pkgs.bash;
    openssh.authorizedKeys.keys = sshKeys;
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys;
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  services.getty.autologinUser = username;
  security.sudo.wheelNeedsPassword = lib.mkForce false;
}
