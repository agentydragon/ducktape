# iguana (ThinkPad X1 Extreme) host-specific home-manager configuration
#
# To apply: sudo nixos-rebuild switch --flake ~/code/ducktape#iguana
{
  config,
  pkgs,
  lib,
  ducktapePackages,
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

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/iguana-forgejo.sops.key;
  ducktape.githubSsh.sopsFile = ../../../ssh_keys/iguana-github.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/iguana-attic.yaml;
  };

  home.packages = [ ducktapePackages.claude-desktop ];

  # ActivityWatch capture + import into the central server. The importer folds this
  # host's buckets into iguana::<bucket> over the bearer-gated write route, using the
  # shared dual-recipient write token the cluster write-proxy checks.
  ducktape.activitywatch.sync = {
    enable = true;
    dest = {
      url = "https://activitywatch-write.allegedly.works";
      device = "iguana";
      tokenSopsFile = ../../../cluster/k8s/x/activitywatch/activitywatch-write-token.sops.yaml;
    };
  };

  home.stateVersion = "24.11";
}
