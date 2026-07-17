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

  ducktape.activitywatch.sync = {
    enable = true;
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

  ducktape.aiquota = {
    enable = true;
    sopsFile = ../../../secrets/shared/zai.yaml;
  };

  # Atlas-specific configuration (Proxmox host with GUI)
  home.stateVersion = "24.05";

  # TODO: HM-level Attic substituter wiring for cache.allegedly.works/{main,gaffer}.
  # Atlas isn't NixOS, so the NixOS-level ducktape.attic-substituter module
  # at nix/nixos/modules/attic-substituter.nix doesn't apply. Reader JWT is
  # already auto-rotated into secrets/hosts/atlas-attic.yaml by the
  # attic-jwt-rotation CronJob (rotators.json entry exists). HM-level
  # wiring would need: nix.settings.{substituters,trusted-public-keys},
  # plus a netrc file rendered into the user's nix config dir, decrypted
  # via home-manager sops. Different shape from the NixOS module because
  # nix.settings on HM only affects the user's nix client, not the daemon.
}
