# Haku Console's chrome-free approvals desktop application.
{
  config,
  ducktapePackages,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.hakuApprovals;
in
{
  options.ducktape.hakuApprovals = {
    enable = lib.mkEnableOption "the Haku Console approvals window";
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ ducktapePackages.hakuApprovals ];

    xdg.autostart = {
      enable = true;
      entries = [
        (pkgs.writeText "haku-approvals.desktop" ''
          [Desktop Entry]
          Type=Application
          Name=Haku Approvals
          Comment=Monitor pending Haku tool-call approvals
          Exec=haku-approvals --background
          Terminal=false
          X-GNOME-Autostart-enabled=true
        '')
      ];
    };
  };
}
