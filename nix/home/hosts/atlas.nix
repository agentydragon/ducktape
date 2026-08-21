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
let
  keys = import ../../ssh-keys.nix;
in
{
  imports = [
    ../home.nix
    ../modules/forgejo-ssh.nix
    ../modules/github-ssh.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/atlas-forgejo.sops.key;
  ducktape.githubSsh.sopsFile = ../../../ssh_keys/atlas-github.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/atlas-attic.yaml;
  };

  ducktape.activitywatch.sync = {
    # Retired with the cluster receiver; retain config material for a future
    # snapshot-based replacement of the aw-sync transport.
    enable = false;
    syncthing = {
      certFile = ../../../secrets/home/atlas/activitywatch-syncthing.cert.pem;
      keySopsFile = ../../../secrets/home/atlas/activitywatch-syncthing.sops.key;
    };
  };

  # Atlas runs on Proxmox VE (Debian-based), not NixOS — no users.users.*.openssh module.
  # home-manager has no authorized_keys option (nix-community/home-manager#4327).
  # home.file creates as 0444 which satisfies sshd (requires not group/world-writable).
  home.file.".ssh/authorized_keys".text = with keys; ''
    ${atlas}
    ${wyrm2}
    ${rugged}
  '';

  ducktape.aiquota.enable = true;
  ducktape.aiquota.remoteApi.enable = true;

  # Atlas-specific configuration (Proxmox host with GUI)
  home.stateVersion = "24.05";

}
