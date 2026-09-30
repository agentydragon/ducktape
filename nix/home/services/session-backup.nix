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
  sqliteSnapshotDirectory = "${config.xdg.cacheHome}/ducktape/ai-session-backup-sqlite";
  sqliteSnapshotScript = pkgs.writeText "snapshot-codex-sqlite.py" (
    builtins.readFile ./snapshot-codex-sqlite.py
  );
  backupScript = pkgs.writeShellScript "ai-session-backup" ''
    set -euo pipefail

    export RESTIC_REPOSITORY=${lib.escapeShellArg "rest:https://restic.allegedly.works/${host}"}
    export RESTIC_REST_USERNAME="$(cat ${config.sops.secrets.ai_session_backup_rest_username.path})"
    export RESTIC_REST_PASSWORD="$(cat ${config.sops.secrets.ai_session_backup_rest_password.path})"
    export RESTIC_PASSWORD="$(cat ${config.sops.secrets.ai_session_backup_restic_password.path})"

    restic=${lib.escapeShellArg "${pkgs.restic}/bin/restic"}
    codex_home=${lib.escapeShellArg codexHome}
    staging_dir=${lib.escapeShellArg sqliteSnapshotDirectory}
    cleanup() {
      rm -rf "$staging_dir"
    }
    trap cleanup EXIT

    rm -rf "$staging_dir"
    mkdir -m 0700 -p "$staging_dir"
    ${pkgs.python3}/bin/python ${sqliteSnapshotScript} "$codex_home" "$staging_dir"

    # The first run initializes this workstation's private repository. A network or
    # authentication failure also reaches init, which fails closed without replacing
    # an existing repository.
    if ! "$restic" snapshots --latest 1 >/dev/null 2>&1; then
      "$restic" init
    fi

    "$restic" backup \
      --host ${lib.escapeShellArg host} \
      --tag ai-sessions \
      --skip-if-unchanged \
      --exclude ${lib.escapeShellArg "${claudeHome}/.credentials.json"} \
      --exclude ${lib.escapeShellArg "${codexHome}/auth.json"} \
      --exclude ${lib.escapeShellArg "${codexHome}/config.toml"} \
      --exclude ${lib.escapeShellArg "${codexHome}/state_*.sqlite*"} \
      --exclude ${lib.escapeShellArg "${codexHome}/cache"} \
      --exclude ${lib.escapeShellArg "${codexHome}/plugins/cache"} \
      ${lib.escapeShellArg claudeHome} \
      "$codex_home" \
      "$staging_dir"
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
