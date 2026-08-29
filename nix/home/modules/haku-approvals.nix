# Haku Console's chrome-free approvals window and GNOME Shell launcher.
{
  config,
  ducktapePackages,
  lib,
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
    home.packages = [ ducktapePackages.haku-approvals ];

    programs.gnome-shell.extensions = [
      { package = ducktapePackages.haku-approvals; }
    ];

  };
}
