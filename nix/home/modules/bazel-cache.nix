# Shared Bazel disk cache across local worktrees and CLI agent sessions.
#
# Each Bazel output base is single-owner (one per worktree/session, locked, and
# not shareable). Bazel already defaults output_base, output_user_root, and the
# downloaded-archive repository_cache into ~/.cache/bazel/_bazel_$USER. This
# module adds the local action cache/CAS for local and no-RBE debugging builds.
#
# Do not enable repo_contents_cache: fetched trees can contain absolute symlinks
# into their producing output base. See devinfra/docs/bazel_worktree_cache_sharing.md.
# This also sets BAZELISK_HOME and the Claude sandbox write entry for it.
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
  bazeliskCache = "${config.xdg.cacheHome}/bazelisk";
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
  };

  config = lib.mkIf cfg.enable {
    # Appends to the base ~/.bazelrc from home.nix (mkAfter keeps the try-import of
    # buildbuddy.bazelrc and common flags first).
    home.file.".bazelrc".text = lib.mkAfter ''
      common --repo_contents_cache=

      build --disk_cache=${bazelDiskCache}
      build --experimental_disk_cache_gc_max_size=${cfg.diskCacheGcMaxSize}
      build --experimental_disk_cache_gc_max_age=14d
    '';

    home.activation.bazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      mkdir -p '${bazelDiskCache}' '${bazeliskCache}'
    '';

    programs.claude-code.settings = {
      env.BAZELISK_HOME = bazeliskCache;
      sandbox.filesystem.allowWrite = lib.mkAfter [ bazeliskCache ];
    };
  };
}
