# Gecko - headless KubeVirt VM for Claude Code / Codex.
#
# Bootstrap flow:
# 1. Boot the generic bootstrap qcow2 from cluster/k8s/gecko.
# 2. Ensure /home/agentydragon/.ssh/id_ed25519 exists.
# 3. Switch to this profile:
#    sudo nixos-rebuild switch --flake ~/code/ducktape#gecko
{
  pkgs,
  ...
}:
{
  imports = [
    ../home.nix
    ../modules/forgejo-ssh.nix
    ../modules/github-ssh.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/gecko-forgejo.sops.key;
  ducktape.githubSsh.sopsFile = ../../../ssh_keys/gecko-github.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/gecko-attic.yaml;
  };

  home.packages = [
    pkgs.psmisc
  ];

  home.stateVersion = "25.11";
}
