# Shared Bazel disk cache across local worktrees and CLI agent sessions.
#
# Each Bazel output base is single-owner (one per worktree/session, locked, and
# not shareable). Bazel already defaults output_base, output_user_root, and the
# downloaded-archive repository_cache into ~/.cache/bazel/_bazel_$USER. This
# module adds the local action cache/CAS for local and no-RBE debugging builds.
#
# Deliberately does not share repo_contents_cache across output bases: fetched
# trees can contain absolute symlinks into their producing output base, so a
# shared entry breaks every consumer once that base is GC'd. Bazel 8.6 leaves
# repo_contents_cache empty (disabled) by default, so no flag is needed to keep
# it off — this module simply never enables it. See
# devinfra/docs/bazel_worktree_cache_sharing.md.
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
      build --disk_cache=${bazelDiskCache}
      build --experimental_disk_cache_gc_max_size=${cfg.diskCacheGcMaxSize}
      build --experimental_disk_cache_gc_max_age=14d
    '';

    home.activation.bazelCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      mkdir -p '${bazelDiskCache}'
    '';
  };
}
