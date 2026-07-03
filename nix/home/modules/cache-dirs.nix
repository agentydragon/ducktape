# Shared Home Manager cache directory materialization.
{ config, lib, ... }:
let
  inherit (config.ducktape) cacheDirs;
  mkCachePath = name: "${config.xdg.cacheHome}/${name}";
in
{
  options.ducktape = {
    cachePaths = {
      bazel = lib.mkOption {
        type = lib.types.str;
        default = mkCachePath "bazel";
        description = "Shared Bazel cache root.";
      };

      bazelisk = lib.mkOption {
        type = lib.types.str;
        default = mkCachePath "bazelisk";
        description = "Bazelisk home/cache directory.";
      };

      codexNpm = lib.mkOption {
        type = lib.types.str;
        default = mkCachePath "codex/npm";
        description = "NPM cache directory used by Codex-run hooks.";
      };

      nix = lib.mkOption {
        type = lib.types.str;
        default = mkCachePath "nix";
        description = "User-owned Nix client cache directory.";
      };

      preCommit = lib.mkOption {
        type = lib.types.str;
        default = mkCachePath "pre-commit";
        description = "Pre-commit cache directory.";
      };
    };

    cacheDirs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      apply = lib.unique;
      description = "User cache directories to create during Home Manager activation.";
    };
  };

  config.home.activation.ducktapeCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] (
    lib.optionalString (cacheDirs != [ ]) ''
      for dir in ${lib.escapeShellArgs cacheDirs}; do
        mkdir -p "$dir"
      done
    ''
  );
}
