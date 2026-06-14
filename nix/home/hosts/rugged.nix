# Dell Rugged 12 tablet - home-manager configuration
{
  config,
  pkgs,
  lib,
  ducktapePackages,
  ...
}:
let
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelRepoContentsCache = "${bazelOutputUserRoot}/cache/repo-contents";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
  bazeliskCache = "${config.xdg.cacheHome}/bazelisk";
in
{
  imports = [
    ../home.nix
    ../modules/15leroy-ssh.nix
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

  ducktape.sopsEnv = {
    ZAI_API_KEY = {
      sopsFile = ../../../secrets/home/rugged/zai.yaml;
      key = "zai_api_key";
    };
  };

  # Rugged-only Bazel cache sharing across local worktrees.
  # See devinfra/docs/bazel_worktree_cache_sharing.md for rationale and probes.
  #
  # Bazel already defaults output_base, output_user_root, and repository_cache to
  # the shared ~/.cache/bazel/_bazel_$USER tree on this host. This only enables
  # caches that are not already enabled by default, while preserving shared
  # .bazelrc customizations from ../home.nix.
  home.file.".bazelrc".text = lib.mkAfter ''
    common --repo_contents_cache=${bazelRepoContentsCache}

    build --disk_cache=${bazelDiskCache}
    build --experimental_disk_cache_gc_max_size=200G
    build --experimental_disk_cache_gc_max_age=14d
  '';

  home.activation.ruggedBazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    mkdir -p '${bazelRepoContentsCache}' '${bazelDiskCache}' '${bazeliskCache}'
  '';

  programs.claude-code.settings = {
    env.BAZELISK_HOME = bazeliskCache;
    sandbox.filesystem.allowWrite = lib.mkAfter [ bazeliskCache ];
  };

  # SSH keys for wyrm and vps, decrypted from SOPS binary at activation time.
  sops.secrets =
    builtins.listToAttrs (
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
    )
    // {
      zai_api_key_file = {
        sopsFile = ../../../secrets/home/rugged/zai.yaml;
        key = "zai_api_key";
      };
    };

  # Wire z.ai API key into aiquota via config.toml (Python CLI reads this).
  xdg.configFile."aiquota/config.toml" = {
    text = ''
      [zai]
      api_key_path = "${config.sops.secrets.zai_api_key_file.path}"
    '';
  };

  # TODO: expose this through an authenticated in-cluster route if rugged's
  # local LLM becomes useful beyond the tablet itself.
  ducktape.opencode.ruggedLocalLlm.enable = true;

  home.packages = [
    ducktapePackages.aiquota
    ducktapePackages.bebas-neue-font
    pkgs.inkscape
    pkgs.kicad
    pkgs.openscad
    ducktapePackages.litert-lm
    pkgs.psmisc
    pkgs.telegram-desktop
    pkgs.lightburn
    ducktapePackages.tana-outliner
  ];
  # NixOS doesn't have Pop!_OS's built-in ubuntu-appindicators, so install it
  programs.gnome-shell.extensions = [
    { package = pkgs.gnomeExtensions.appindicator; }
    { package = ducktapePackages.aiquota; }
  ];

  # Enable GNOME fractional scaling (125/150/175%). GNOME gates these steps
  # behind this experimental flag; without it Settings only offers 100%/200%.
  dconf.settings."org/gnome/mutter".experimental-features = [
    "scale-monitor-framebuffer"
  ];

  # gaffer-private disabled — see nix/docs/private_flake_inputs.md
  # services.google-drive.enable = true;

  home.stateVersion = "25.11";
}
