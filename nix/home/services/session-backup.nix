{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.sessionBackup;
  home = config.home.homeDirectory;
  claudeHome = config.home.sessionVariables.CLAUDE_CONFIG_DIR or "${home}/.claude";
  codexHome =
    config.home.sessionVariables.CODEX_HOME or (
      if config.home.preferXdgDirectories then "${config.xdg.configHome}/codex" else "${home}/.codex"
    );
  host = if cfg.host == null then "invalid-host" else cfg.host;
  # All three personal machines can decrypt these so any of them can restore another host.
  credentialsFile = ../../../secrets/shared/session-backup.yaml;
  sqliteSnapshotDirectory = "${config.xdg.stateHome}/ducktape/ai-session-backup-sqlite";
  sqliteSnapshotScript = pkgs.writeText "snapshot_sqlite.py" (builtins.readFile ./snapshot_sqlite.py);
  backupScript = pkgs.writeShellScript "ai-session-backup" ''
    set -euo pipefail

    export RESTIC_REPOSITORY=${lib.escapeShellArg "rest:https://restic.allegedly.works/${host}"}
    export RESTIC_REST_USERNAME="$(cat ${config.sops.secrets.ai_session_backup_rest_username.path})"
    export RESTIC_REST_PASSWORD="$(cat ${config.sops.secrets.ai_session_backup_rest_password.path})"
    export RESTIC_PASSWORD_FILE=${lib.escapeShellArg config.sops.secrets.ai_session_backup_restic_password.path}
    exec ${pkgs.bash}/bin/bash ${./session_backup.sh} \
      ${pkgs.restic}/bin/restic \
      ${pkgs.python3}/bin/python \
      ${sqliteSnapshotScript} \
      ${lib.escapeShellArg home} \
      ${lib.escapeShellArg claudeHome} \
      ${lib.escapeShellArg codexHome} \
      ${lib.escapeShellArg sqliteSnapshotDirectory} \
      ${lib.escapeShellArg host}
  '';
in
{
  options.ducktape.sessionBackup = {
    enable = lib.mkEnableOption "encrypted Restic backups of Claude Code and Codex session data";

    host = lib.mkOption {
      type = lib.types.nullOr (
        lib.types.enum [
          "wyrm2"
          "rugged"
          "iguana"
        ]
      );
      default = null;
      description = "The workstation's private Restic repository identity.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ pkgs.restic ];

    assertions = [
      {
        assertion = cfg.host != null;
        message = "ducktape.sessionBackup.enable requires a workstation host identity.";
      }
    ];

    sops.secrets = {
      ai_session_backup_rest_username = {
        sopsFile = credentialsFile;
        key = "restic_rest_username_${host}";
        mode = "0400";
      };
      ai_session_backup_rest_password = {
        sopsFile = credentialsFile;
        key = "restic_rest_password_${host}";
        mode = "0400";
      };
      ai_session_backup_restic_password = {
        sopsFile = credentialsFile;
        key = "restic_password";
        mode = "0400";
      };
    };

    systemd.user.services.ai-session-backup = {
      Unit = {
        Description = "Back up Claude Code and Codex sessions to the in-cluster Restic server";
        After = [
          "network-online.target"
          "sops-nix.service"
        ];
        Requires = [ "sops-nix.service" ];
      };
      Service = {
        Type = "oneshot";
        ExecStart = backupScript;
        WorkingDirectory = home;
        RuntimeDirectory = "ai-session-backup";
        RuntimeDirectoryMode = "0700";
        TimeoutStartSec = "6h";
        UMask = "0077";
        NoNewPrivileges = true;
      };
    };

    systemd.user.timers.ai-session-backup = {
      Unit.Description = "Periodic encrypted backup of local AI sessions";
      Timer = {
        OnBootSec = "2min";
        OnCalendar = "*:0/15";
        Persistent = true;
        Unit = "ai-session-backup.service";
      };
      Install.WantedBy = [ "timers.target" ];
    };
  };
}
