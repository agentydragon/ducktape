# iguana (ThinkPad X1 Extreme) host-specific home-manager configuration
#
# Previously Pop!_OS (agentydragon.nix), now NixOS.
#
# To apply: sudo nixos-rebuild switch --flake ~/code/ducktape#iguana
{
  config,
  pkgs,
  lib,
  ...
}:
{
  imports = [
    ../home.nix
    ../modules/bazel-user-cache.nix
    ../modules/forgejo-ssh.nix
    ../modules/github-ssh.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/iguana-forgejo.sops.key;
  ducktape.githubSsh.sopsFile = ../../../ssh_keys/iguana-github.sops.key;
  ducktape.bazelUserCache.enable = true;

  # AppIndicator support — needed for timekpr-client tray icon in GNOME.
  programs.gnome-shell.extensions = [
    { package = pkgs.gnomeExtensions.appindicator; }
  ];

  home.stateVersion = "24.11";
}
