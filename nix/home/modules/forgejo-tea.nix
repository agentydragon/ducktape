# Forgejo API token for the `tea` CLI.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.forgejoTea;
in
{
  options.ducktape.forgejoTea = {
    enable = lib.mkEnableOption "Forgejo tea CLI config";
    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to the SOPS-encrypted YAML file containing token, username, and url keys.";
    };
    loginName = lib.mkOption {
      type = lib.types.str;
      default = "forgejo";
      description = "tea login name.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.forgejo_tea_token = {
      inherit (cfg) sopsFile;
      key = "token";
    };
    sops.secrets.forgejo_tea_username = {
      inherit (cfg) sopsFile;
      key = "username";
    };
    sops.secrets.forgejo_tea_url = {
      inherit (cfg) sopsFile;
      key = "url";
    };

    sops.templates."tea-config.yml" = {
      path = "${config.xdg.configHome}/tea/config.yml";
      content = ''
        logins:
          - name: ${cfg.loginName}
            url: ${config.sops.placeholder.forgejo_tea_url}
            token: ${config.sops.placeholder.forgejo_tea_token}
            default: true
            version_check: false
            user: ${config.sops.placeholder.forgejo_tea_username}
      '';
      mode = "0600";
    };
  };
}
