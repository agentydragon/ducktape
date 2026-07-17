# AI quota tracker — CLI + GNOME Shell extension + z.ai API key wiring.
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
    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "SOPS-encrypted YAML containing `zai_api_key`.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.zai_api_key_file = {
      inherit (cfg) sopsFile;
      key = "zai_api_key";
    };

    xdg.configFile."aiquota/config.toml".text = ''
      [zai]
      api_key_path = "${config.sops.secrets.zai_api_key_file.path}"
    '';

    home.packages = [ ducktapePackages.aiquota ];

    programs.gnome-shell.extensions = [
      { package = ducktapePackages.aiquota; }
    ];
  };
}
