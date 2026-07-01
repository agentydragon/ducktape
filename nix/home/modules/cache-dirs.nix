# Shared Home Manager cache directory materialization.
{ config, lib, ... }:
let
  inherit (config.ducktape) cacheDirs;
in
{
  options.ducktape.cacheDirs = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [ ];
    apply = lib.unique;
    description = "User cache directories to create during Home Manager activation.";
  };

  config.home.activation.ducktapeCacheDirs = lib.hm.dag.entryAfter [ "writeBoundary" ] (
    lib.optionalString (cacheDirs != [ ]) ''
      for dir in ${lib.escapeShellArgs cacheDirs}; do
        mkdir -p "$dir"
      done
    ''
  );
}
