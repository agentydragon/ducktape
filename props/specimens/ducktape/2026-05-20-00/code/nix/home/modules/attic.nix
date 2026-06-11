# Attic binary cache credentials.
# Decrypts the token from SOPS using ~/.ssh/id_ed25519 and renders
# ~/.config/attic/config.toml via sops-nix templates.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.attic;
in
{
  options.ducktape.attic = {
    enable = lib.mkEnableOption "Attic cache credentials";
    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to the SOPS-encrypted YAML file containing the attic_token.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.attic_token = {
      inherit (cfg) sopsFile;
    };

    sops.templates."attic-config.toml" = {
      path = "${config.xdg.configHome}/attic/config.toml";
      content = ''
        default-server = "main"

        [servers.main]
        endpoint = "https://cache.allegedly.works"
        token = "${config.sops.placeholder.attic_token}"
      '';
      mode = "0600";
    };
  };
}
