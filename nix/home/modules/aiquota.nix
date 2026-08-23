# AI quota tracker — CLI + GNOME Shell extension.
{
  config,
  lib,
  ducktapePackages,
  ...
}:
let
  cfg = config.ducktape.aiquota;
in
{
  options.ducktape.aiquota = {
    enable = lib.mkEnableOption "aiquota AI quota tracker";

    remoteApi = {
      enable = lib.mkEnableOption "the in-cluster aiquota API";
      url = lib.mkOption {
        type = lib.types.str;
        default = "https://aiquota.allegedly.works";
        description = "URL of the bearer-authenticated in-cluster aiquota API.";
      };
      sopsFile = lib.mkOption {
        type = lib.types.path;
        default = ../../../cluster/k8s/aiquota/aiquota-api-bearer.sops.yaml;
        description = "SOPS file containing the aiquota API bearer token.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ ducktapePackages.aiquota ];

    programs.gnome-shell.extensions = [
      { package = ducktapePackages.aiquota; }
    ];

    sops.secrets.aiquota_api_bearer = lib.mkIf cfg.remoteApi.enable {
      sopsFile = cfg.remoteApi.sopsFile;
      key = "stringData/bearer-token";
    };

    sops.templates."aiquota-config.toml" = lib.mkIf cfg.remoteApi.enable {
      path = "${config.xdg.configHome}/aiquota/config.toml";
      mode = "0600";
      content = ''
        [remote_api]
        url = "${cfg.remoteApi.url}"
        bearer_token = "${config.sops.placeholder.aiquota_api_bearer}"
      '';
    };
  };
}
