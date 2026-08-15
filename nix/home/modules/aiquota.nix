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
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ ducktapePackages.aiquota ];

    programs.gnome-shell.extensions = [
      { package = ducktapePackages.aiquota; }
    ];
  };
}
