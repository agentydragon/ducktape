# Generic bootstrap NixOS — minimal SSH-able image for KubeVirt / Proxmox VMs.
# After first boot, deploy the real host config:
#   nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#<host>
#
# Aggressively trimmed for closure size — the bootstrap image is uploaded to
# SeaweedFS and pulled by CDI on every VM provision. See
# <cluster/docs/kubevirt_vm_image_artifacts.md>.
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
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICweiJQQidbhojDI7tXuSuntptCc6Dy4stIGzDlI9z0b agentydragon@rugged"
  ];
in
{
  imports = [ ../../modules/operator.nix ];

  # VMs use virtio devices only — no need to ship the full firmware tree.
  hardware.enableAllFirmware = lib.mkForce false;
  hardware.enableRedistributableFirmware = lib.mkForce false;

  # NetworkManager isn't needed for a single virtio NIC; use systemd-networkd.
  networking.networkmanager.enable = lib.mkForce false;
  networking.useNetworkd = true;
  systemd.network.networks."10-default" = {
    matchConfig.Name = "en*";
    networkConfig.DHCP = "yes";
  };

  # KubeVirt attaches a NoCloud seed for per-VM bootstrap data. Gecko uses this
  # to install its persisted Ed25519 SSH host key before sshd starts.
  services.cloud-init = {
    enable = true;
    network.enable = false;
    settings.datasource_list = [ "NoCloud" ];
  };

  # Don't vendor a nixpkgs source tree — this image only ever runs flake
  # commands by pinned URL, so the channel, NIX_PATH, and the flake registry
  # mapping for `nixpkgs` are all dead weight.
  nix.channel.enable = false;
  nix.nixPath = lib.mkForce [ ];
  nix.registry = lib.mkForce { };

  # No manuals/docs in the bootstrap image.
  documentation = {
    enable = false;
    man.enable = false;
    nixos.enable = false;
    doc.enable = false;
    info.enable = false;
  };

  users.users.${username} = {
    shell = pkgs.bash;
    openssh.authorizedKeys.keys = sshKeys;
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys;
  services.openssh.hostKeys = lib.mkForce [
    {
      type = "ed25519";
      path = "/etc/ssh/ssh_host_ed25519_key";
    }
  ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  services.getty.autologinUser = username;
  security.sudo.wheelNeedsPassword = lib.mkForce false;
}
