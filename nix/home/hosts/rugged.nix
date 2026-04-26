# Dell Rugged 12 tablet - home-manager configuration
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
    ../modules/15leroy-ssh.nix
    ../modules/github-ssh.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
    ../modules/discord-minimized-autostart.nix
  ];

  ducktape.githubSsh.sopsFile = ../../../ssh_keys/rugged-github.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/home/rugged/attic.yaml;
  };

  # SSH keys for wyrm and vps, decrypted from SOPS binary at activation time.
  sops.secrets = builtins.listToAttrs (
    map
      (
        {
          name,
          sopsFile,
          filename,
        }:
        {
          inherit name;
          value = {
            sopsFile = ../../../ssh_keys/${sopsFile};
            format = "binary";
            path = "${config.home.homeDirectory}/.ssh/${filename}";
            mode = "0600";
          };
        }
      )
      [
        {
          name = "wyrm_ssh_key";
          sopsFile = "rugged-wyrm.sops.key";
          filename = "wyrm_agentydragon_user_id_ed25519";
        }
        {
          name = "vps_root_ssh_key";
          sopsFile = "rugged-vps-root.sops.key";
          filename = "vps_root_id_ed25519";
        }
        {
          name = "vps_user_ssh_key";
          sopsFile = "rugged-vps-user.sops.key";
          filename = "vps_agentydragon_user_id_ed25519";
        }
      ]
  );

  home.packages = [
    ducktapePackages.bebas-neue-font
    pkgs.inkscape
    pkgs.kicad
    pkgs.openscad
    pkgs.psmisc
    pkgs.telegram-desktop
    pkgs.lightburn
    ducktapePackages.tana
  ];
  # NixOS doesn't have Pop!_OS's built-in ubuntu-appindicators, so install it
  programs.gnome-shell.extensions = [
    { package = pkgs.gnomeExtensions.appindicator; }
  ];

  services.google-drive.enable = true;

  home.stateVersion = "25.11";
}
