# Shared Bazel caches across local worktrees and CLI agent sessions.
#
# Each Bazel output base is single-owner (one per worktree/session, locked, and
# not shareable). Bazel already defaults output_base, output_user_root, and the
# downloaded-archive repository_cache into ~/.cache/bazel/_bazel_$USER. This
# module adds the local action cache/CAS for local and no-RBE debugging builds.
# Bazel 9 also has a local repo_contents_cache; hosts can pin its path and GC
# policy here. See devinfra/docs/bazel_worktree_cache_sharing.md for its scope
# and the remaining symlink-safety follow-up.
#
# The bazelisk binary cache needs no wiring here: it already defaults to the
# user-global ~/.cache/bazelisk, and its Claude-sandbox write grant lives with
# the other sandbox writes in nix/home/claude_code/default.nix.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.bazelCache;
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
  bazelRepositoryContentsCache = "${bazelOutputUserRoot}/cache/repos/v1/contents";
in
{
  options.ducktape.bazelCache = {
    enable = lib.mkEnableOption "shared Bazel disk cache across local worktrees";

    diskCacheGcMaxSize = lib.mkOption {
      type = lib.types.str;
      default = "200G";
      example = "80G";
      description = ''
        Value for Bazel's --experimental_disk_cache_gc_max_size. Lower it on hosts whose
        ~/.cache/bazel/_bazel_$USER/cache/disk shares a smaller SSD with the output bases.
      '';
    };

    repoContentsCache = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = bazelRepositoryContentsCache;
      description = ''
        Optional explicit path for Bazel's --repo_contents_cache. Null leaves
        Bazel's version-specific default unchanged.
      '';
    };

    repoContentsCacheGcMaxAge = lib.mkOption {
      type = lib.types.str;
      default = "14d";
      description = "Value for Bazel's --repo_contents_cache_gc_max_age.";
    };

    repoContentsCacheGcIdleDelay = lib.mkOption {
      type = lib.types.str;
      default = "5m";
      description = "Value for Bazel's --repo_contents_cache_gc_idle_delay.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Appends to the base ~/.bazelrc from home.nix (mkAfter keeps the try-import of
    # buildbuddy.bazelrc and common flags first).
    home.file.".bazelrc".text = lib.mkAfter ''
      build --disk_cache=${bazelDiskCache}
      build --experimental_disk_cache_gc_max_size=${cfg.diskCacheGcMaxSize}
      build --experimental_disk_cache_gc_max_age=14d
      ${lib.optionalString (cfg.repoContentsCache != null) ''
        common --repo_contents_cache=${cfg.repoContentsCache}
        common --repo_contents_cache_gc_max_age=${cfg.repoContentsCacheGcMaxAge}
        common --repo_contents_cache_gc_idle_delay=${cfg.repoContentsCacheGcIdleDelay}
      ''}
    '';

    home.activation.bazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      mkdir -p '${bazelDiskCache}'
      ${lib.optionalString (cfg.repoContentsCache != null) ''
        mkdir -p '${cfg.repoContentsCache}'
      ''}
    '';
  };
}
