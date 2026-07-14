# Shared Bazel disk + repo-contents cache across local worktrees and CLI agent sessions.
#
# Each Bazel output base is single-owner (one per worktree/session, locked, can't be shared), so
# without this each base independently extracts external repos into its own external/ and re-runs
# local actions — the bulk of the disk that piles up across worktrees. Bazel already defaults
# output_base, output_user_root, and repository_cache (downloaded archives) into the shared
# ~/.cache/bazel/_bazel_$USER tree; this enables the two caches that are NOT on by default:
#
#   --repo_contents_cache  — share the EXTRACTED external repos (the big per-base win)
#   --disk_cache           — local action cache/CAS (helps local / no-RBE debug builds)
#
# plus BAZELISK_HOME and the Claude sandbox write entry for the bazelisk cache. See
# devinfra/docs/bazel_worktree_cache_sharing.md for rationale, probes, and the cross-filesystem
# hardlink caveat (keep --experimental_repository_cache_hardlinks OFF where the repo cache and the
# output bases live on different filesystems, e.g. wyrm2).
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.bazelCache;
  bazelCacheRoot = "${config.xdg.cacheHome}/bazel";
  bazelOutputUserRoot = "${bazelCacheRoot}/_bazel_${config.home.username}";
  bazelRepoContentsCache = "${bazelOutputUserRoot}/cache/repo-contents";
  bazelDiskCache = "${bazelOutputUserRoot}/cache/disk";
  bazeliskCache = "${config.xdg.cacheHome}/bazelisk";
in
{
  options.ducktape.bazelCache = {
    enable = lib.mkEnableOption "shared Bazel disk + repo-contents cache across local worktrees";

    diskCacheGcMaxSize = lib.mkOption {
      type = lib.types.str;
      default = "200G";
      example = "80G";
      description = ''
        Value for Bazel's --experimental_disk_cache_gc_max_size. Lower it on hosts whose
        ~/.cache/bazel/_bazel_$USER/cache/disk shares a smaller SSD with the output bases.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # Appends to the base ~/.bazelrc from home.nix (mkAfter keeps the try-import of
    # buildbuddy.bazelrc and common flags first).
    home.file.".bazelrc".text = lib.mkAfter ''
      common --repo_contents_cache=${bazelRepoContentsCache}

      build --disk_cache=${bazelDiskCache}
      build --experimental_disk_cache_gc_max_size=${cfg.diskCacheGcMaxSize}
      build --experimental_disk_cache_gc_max_age=14d
    '';

    home.activation.bazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      mkdir -p '${bazelRepoContentsCache}' '${bazelDiskCache}' '${bazeliskCache}'
    '';

    programs.claude-code.settings = {
      env.BAZELISK_HOME = bazeliskCache;
      sandbox.filesystem.allowWrite = lib.mkAfter [ bazeliskCache ];
    };
  };
}
