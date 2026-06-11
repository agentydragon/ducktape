# SOPS-decrypted environment variables.
# Each entry declares a sops secret and exports it as an env var via shell init.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.sopsEnv;

  envType = lib.types.submodule {
    options = {
      sopsFile = lib.mkOption {
        type = lib.types.path;
        description = "Path to the SOPS-encrypted YAML file.";
      };
      key = lib.mkOption {
        type = lib.types.str;
        description = "Key within the SOPS file to decrypt.";
      };
    };
  };

  enabledVars = cfg;

  shellExports = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (
      envVar: opts: ''export ${envVar}="$(cat ${config.sops.secrets.${opts.key}.path})"''
    ) enabledVars
  );
in
{
  options.ducktape.sopsEnv = lib.mkOption {
    type = lib.types.attrsOf envType;
    default = { };
    description = "Environment variables to export from SOPS-decrypted secrets.";
  };

  config = lib.mkIf (enabledVars != { }) {
    sops.secrets = lib.mapAttrs' (
      _envVar: opts:
      lib.nameValuePair opts.key {
        inherit (opts) sopsFile;
      }
    ) enabledVars;

    programs.zsh.initContent = shellExports;
    programs.bash.initExtra = shellExports;
  };
}
