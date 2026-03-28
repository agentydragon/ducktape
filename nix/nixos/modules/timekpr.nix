# Timekpr-nExT screen time management with declarative per-user configuration
#
# Wraps the upstream services.timekpr module and adds a systemd oneshot
# to apply per-user allowed hours and lockout type via timekpra CLI.
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.ducktape.timekpr;

  # Render allowedHours blocks into the timekpra hours string format.
  # Example output: "22[00-50];23[00-50];0[00-50];1[00-15];...;21[00-60]"
  renderHoursSpec =
    allowedHours:
    lib.concatStringsSep ";" (
      lib.concatMap (block: map (h: "${toString h}[${block.minuteRange}]") block.hours) allowedHours
    );

  # Generate shell commands for a single user
  userCommands =
    username: userCfg:
    let
      hoursSpec = renderHoursSpec userCfg.allowedHours;
      timekpra = lib.getExe' cfg.package "timekpra";
    in
    ''
      ${timekpra} --setallowedhours ${username} ALL "${hoursSpec}"
      ${timekpra} --setlockouttype ${username} ${userCfg.lockoutType}
    '';

  allCommands = lib.concatStringsSep "\n" (lib.mapAttrsToList userCommands cfg.users);

  hourBlockType = lib.types.submodule {
    options = {
      hours = lib.mkOption {
        type = lib.types.listOf lib.types.int;
        description = "Hours of the day (0-23) this block applies to.";
      };
      minuteRange = lib.mkOption {
        type = lib.types.str;
        description = ''
          Minute range within each hour, in timekpr format.
          "00-60" = full hour allowed.
          "00-50" = minutes 0-50 allowed (forced 10-min pause).
          "00-15" = only minutes 0-15 allowed.
        '';
      };
    };
  };

  userType = lib.types.submodule {
    options = {
      lockoutType = lib.mkOption {
        type = lib.types.enum [
          "lock"
          "suspend"
          "suspendwake"
          "terminate"
          "shutdown"
        ];
        default = "lock";
        description = "Action when time runs out.";
      };

      allowedHours = lib.mkOption {
        type = lib.types.listOf hourBlockType;
        description = "Allowed hours specification, applied to ALL weekdays.";
      };
    };
  };
in
{
  options.ducktape.timekpr = {
    enable = lib.mkEnableOption "timekpr-next screen time management";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.timekpr;
      defaultText = lib.literalExpression "pkgs.timekpr";
      description = "The timekpr package to use.";
    };

    users = lib.mkOption {
      type = lib.types.attrsOf userType;
      default = { };
      description = "Per-user timekpr configuration.";
    };
  };

  config = lib.mkIf cfg.enable {
    services.timekpr = {
      enable = true;
      inherit (cfg) package;
    };

    # Autostart the client (tray icon + countdown notifications) in GNOME sessions
    environment.etc."xdg/autostart/timekpr-client.desktop".source =
      "${cfg.package}/etc/xdg/autostart/timekpr-client.desktop";

    # Oneshot to apply per-user config after the daemon starts.
    # timekpra communicates over D-Bus, so the daemon must be running.
    systemd.services.timekpr-configure = lib.mkIf (cfg.users != { }) {
      description = "Apply declarative timekpr user configuration";
      after = [ "timekpr.service" ];
      wants = [ "timekpr.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = pkgs.writeShellScript "timekpr-configure" allCommands;
      };
    };
  };
}
