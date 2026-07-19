# Dell Rugged 12 tablet - home-manager configuration
{
  config,
  pkgs,
  ducktapePackages,
  ...
}:
{
  imports = [
    ../home.nix
    ../modules/bazel-cache.nix
    ../modules/forgejo-ssh.nix
    ../modules/github-ssh.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
    ../modules/discord-minimized-autostart.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/rugged-forgejo.sops.key;
  ducktape.githubSsh.sopsFile = ../../../ssh_keys/rugged-github.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/home/rugged/attic.yaml;
  };

  # Shared Bazel disk cache across local worktrees
  # (see ../modules/bazel-cache.nix). 200G is the module default.
  ducktape.bazelCache.enable = true;

  ducktape.sopsEnv = {
    GROQ_API_KEY = {
      sopsFile = ../../../secrets/home/rugged/groq.yaml;
      key = "groq_api_key";
    };
  };

  ducktape.activitywatch.sync = {
    enable = true;
    syncthing = {
      certFile = ../../../secrets/home/rugged/activitywatch-syncthing.cert.pem;
      keySopsFile = ../../../secrets/home/rugged/activitywatch-syncthing.sops.key;
    };
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

  ducktape.aiquota = {
    enable = true;
    sopsFile = ../../../secrets/shared/zai.yaml;
  };

  # TODO: expose this through an authenticated in-cluster route if rugged's
  # local LLM becomes useful beyond the tablet itself.
  ducktape.opencode.ruggedLocalLlm.enable = true;

  home.packages = [
    ducktapePackages.bebas-neue-font
    ducktapePackages.claude-desktop
    pkgs.freerdp3 # RDP client for wyrm2's xrdp over the SSH tunnel
    pkgs.moonlight-qt # Sunshine client (GPU stream) for wyrm2 when logged in
    pkgs.inkscape
    pkgs.kicad
    pkgs.openscad
    ducktapePackages.litert-lm
    pkgs.psmisc
    pkgs.telegram-desktop
    pkgs.lightburn
    ducktapePackages.tana-outliner
  ];

  # Enable GNOME fractional scaling (125/150/175%). GNOME gates these steps
  # behind this experimental flag; without it Settings only offers 100%/200%.
  dconf.settings."org/gnome/mutter".experimental-features = [
    "scale-monitor-framebuffer"
  ];

  # drivefs is provided by gaffer-private CI via cache.allegedly.works/gaffer
  # (per nix/gaffer-pins.json + nix/packages/gaffer.nix). Substituted, never
  # built from source on the consumer side.
  services.google-drive.enable = true;

  home.stateVersion = "25.11";
}
