# Shared per-user Bazel caches across repos and worktrees.
{
  config,
  lib,
  options,
  ...
}:
let
  cfg = config.ducktape.bazelUserCache;
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelRepoContentsCache = "${bazelOutputUserRoot}/cache/repo-contents";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
  bazeliskCache = "${config.xdg.cacheHome}/bazelisk";
  hasClaudeCode = builtins.hasAttr "claude-code" options.programs;
in
{
  options.ducktape.bazelUserCache = {
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

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        home.file.".bazelrc".text = lib.mkAfter ''
          common --repo_contents_cache=${bazelRepoContentsCache}

          build --disk_cache=${bazelDiskCache}
          build --experimental_disk_cache_gc_max_size=${cfg.diskCacheMaxSize}
          build --experimental_disk_cache_gc_max_age=${cfg.diskCacheMaxAge}
        '';

        home.activation.bazelUserCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          mkdir -p '${bazelRepoContentsCache}' '${bazelDiskCache}' '${bazeliskCache}'
        '';

        home.sessionVariables.BAZELISK_HOME = bazeliskCache;
      }

      (lib.mkIf hasClaudeCode {
        programs.claude-code.settings = {
          env.BAZELISK_HOME = bazeliskCache;
          sandbox.filesystem.allowWrite = lib.mkAfter [ bazeliskCache ];
        };
      })
    ]
  );
}
