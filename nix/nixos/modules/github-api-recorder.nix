# Connection attempts across the node, plus periodic established TCP counters in
# the host network namespace. Persistent HTTP/2 connections can carry API traffic
# without producing another connection event. Neither source measures GraphQL cost.
# Logs stay on the host because they include every peer; the journal ships to Loki.
#
# See <debug/github_graphql_exhaustion/README.md>.
#
# CLEANUP(added 2026-09-04): remove this module and its host opt-ins once GitHub
# issue #5213 identifies the consumer and the attribution investigation is closed.
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.ducktape.githubApiRecorder;
  logDir = "/var/log/tcp-connect-recorder";
  logFile = "${logDir}/connections.log";
  socketLogFile = "${logDir}/sockets.log";
in
{
  options.ducktape.githubApiRecorder = {
    enable = lib.mkEnableOption "connection and established TCP activity recording for GitHub API attribution";

    retentionDays = lib.mkOption {
      type = lib.types.int;
      default = 14;
      description = "Rotated daily logs to keep. The investigation needs a few days of overlap with a burn.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Readable by the operator's primary group, not world: the log is a complete
    # record of every peer this machine dials, so it stays off other accounts,
    # but analysing it should not need root. `z` adjusts the file systemd creates
    # through `StandardOutput=append:`, which would otherwise be 0644 root:root.
    systemd.tmpfiles.rules = [
      "d ${logDir} 0750 root users -"
      "z ${logFile} 0640 root users -"
      "f ${socketLogFile} 0640 root users -"
    ];

    systemd.services.github-api-recorder = {
      description = "Outbound TCP connection recorder (GitHub API attribution)";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];
      # -B line inside the wrapper: bpftrace's stdout is a pipe and then a file, so
      # without line buffering records sit in libc's block buffer instead of
      # reaching the log.
      path = [
        pkgs.bpftrace
        pkgs.gnugrep
      ];
      serviceConfig = {
        Type = "simple";
        ExecStart = "${lib.getExe pkgs.bash} ${./github-api-recorder.sh} ${./github-api-recorder.bt}";
        Restart = "on-failure";
        RestartSec = 30;
        StandardOutput = "append:${logFile}";
        StandardError = "journal";
      };
    };

    systemd.services.github-api-socket-snapshot = {
      description = "Established TCP socket counters (GitHub API attribution)";
      path = [
        pkgs.coreutils
        pkgs.iproute2
      ];
      serviceConfig = {
        Type = "oneshot";
        TimeoutStartSec = "10s";
        ExecStart = "${lib.getExe pkgs.bash} ${./github-api-socket-snapshot.sh}";
        StandardOutput = "append:${socketLogFile}";
        StandardError = "journal";
        UMask = "0027";
      };
    };

    systemd.timers.github-api-socket-snapshot = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "30s";
        OnUnitActiveSec = "30s";
        AccuracySec = "1s";
      };
    };

    # copytruncate: systemd holds the append fd open for the service's lifetime, so
    # renaming the file would leave bpftrace writing to an unlinked inode.
    services.logrotate.settings.github-api-recorder = {
      files = [
        logFile
        socketLogFile
      ];
      frequency = "daily";
      rotate = cfg.retentionDays;
      compress = true;
      missingok = true;
      notifempty = true;
      copytruncate = true;
    };
  };
}
