# User-level Bazel configuration shared across repos and worktrees.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.bazel;
  inherit (cfg) userCache;
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelRepoContentsCache = "${bazelOutputUserRoot}/cache/repo-contents";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
  bazeliskCache = "${config.xdg.cacheHome}/bazelisk";
in
{
  options.ducktape.bazel = {
    enable = lib.mkEnableOption "user-level Bazel configuration";

    progress.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to include interactive Bazel progress settings in ~/.bazelrc.";
    };

    buildbuddyConfig.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether ~/.bazelrc should import the generated BuildBuddy credentials bazelrc.";
    };

    userCache = {
      enable = lib.mkEnableOption "shared per-user Bazel caches across repos and worktrees";

      diskCacheMaxSize = lib.mkOption {
        type = lib.types.str;
        default = "200G";
        description = "Maximum size for Bazel's shared local disk cache.";
      };

      diskCacheMaxAge = lib.mkOption {
        type = lib.types.str;
        default = "14d";
        description = "Maximum age for entries in Bazel's shared local disk cache.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.file.".bazelrc".text = lib.concatStringsSep "\n" (
      lib.filter (section: section != "") [
        (lib.optionalString cfg.progress.enable ''
          common --show_progress_rate_limit=0.05
          common --progress_in_terminal_title
        '')
        (lib.optionalString cfg.buildbuddyConfig.enable ''
          try-import ${config.home.homeDirectory}/.config/bazel/buildbuddy.bazelrc
        '')
        (lib.optionalString userCache.enable ''
          common --repo_contents_cache=${bazelRepoContentsCache}

          build --disk_cache=${bazelDiskCache}
          build --experimental_disk_cache_gc_max_size=${userCache.diskCacheMaxSize}
          build --experimental_disk_cache_gc_max_age=${userCache.diskCacheMaxAge}
        '')
      ]
    );

    home.activation.bazelUserDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      mkdir -p '${bazelCacheRoot}' '${bazeliskCache}' ${lib.optionalString userCache.enable "'${bazelRepoContentsCache}' '${bazelDiskCache}'"}
    '';

    home.sessionVariables.BAZELISK_HOME = bazeliskCache;
  };
}
