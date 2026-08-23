# SOPS-decrypted environment variables.
# Each entry declares a sops secret and exports it as an env var via shell init.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.sopsEnv;

  envType = lib.types.submodule (
    { config, ... }:
    {
      options = {
        sopsFile = lib.mkOption {
          type = lib.types.path;
          description = "Path to the SOPS-encrypted YAML file.";
        };
        key = lib.mkOption {
          type = lib.types.str;
          description = ''
            Key within the SOPS file to decrypt. Nested keys use `/` as a
            separator, so a k8s Secret manifest can be read directly
            (`stringData/api-key`) rather than copied to a flat file.
          '';
        };
        name = lib.mkOption {
          type = lib.types.str;
          default = builtins.replaceStrings [ "/" ] [ "_" ] config.key;
          description = ''
            Attribute name of the underlying sops-nix secret, which also names
            the decrypted file. Defaults to `key` with separators flattened,
            since a nested key would otherwise become a directory path.
          '';
        };
      };
    }
  );

  enabledVars = cfg;

  shellExports = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (
      envVar: opts: ''export ${envVar}="$(cat ${config.sops.secrets.${opts.name}.path})"''
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
      lib.nameValuePair opts.name {
        inherit (opts) sopsFile key;
      }
    ) enabledVars;

    programs.zsh.initContent = shellExports;
    programs.bash.initExtra = shellExports;
  };
}
