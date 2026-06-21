# Google Drive File Stream as a home-manager systemd-user service.
#
# The drivefs and drivectl binaries are provided by gaffer-private's CI through the cluster
# attic cache (cache.allegedly.works/gaffer); it lands in this config via
# `gafferPkgs`, which resolves to `builtins.fetchClosure` entries in
# `nix/packages/gaffer.nix` driven by `nix/gaffer-pins.json`. No
# gaffer-private source is fetched on the consumer host.
#
# Adapted from gaffer-private's drivefs/google-drive-service.nix.
{
  config,
  lib,
  gafferPkgs,
  ...
}:
let
  cfg = config.services.google-drive;
  drivePackages = [
    gafferPkgs.drivefs
  ]
  ++ lib.optionals (gafferPkgs ? drivectl) [ gafferPkgs.drivectl ];
in
{
  options.services.google-drive = {
    enable = lib.mkEnableOption "Google Drive File Stream service";

    mountPoint = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/.google-drive";
      description = "FUSE mount point for Google Drive.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = drivePackages;

    systemd.user.services.google-drive = {
      Unit = {
        Description = "Google Drive File Stream";
        After = [ "network-online.target" ];
      };
      Service = {
        Type = "simple";
        Restart = "on-failure";
        RestartSec = 10;
        ExecStart = "${gafferPkgs.drivefs}/bin/google-drive ${cfg.mountPoint}";
        ExecStop = "${gafferPkgs.drivefs}/bin/google-drive --push_changes_and_quit";
        StandardOutput = "journal";
        StandardError = "journal";
      };
      Install = {
        WantedBy = [ "default.target" ];
      };
    };

    # ~/drive -> mount point's "My Drive" (chains through HM store path)
    home.file."drive".source = config.lib.file.mkOutOfStoreSymlink "${cfg.mountPoint}/My Drive";

    # The daemon installs the Chrome native messaging manifest itself
    # (InstallSwitchbladeNativeHostManifest) when it detects Chrome.
  };
}
