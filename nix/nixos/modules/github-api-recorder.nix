# Records which local process opens each outbound TCP connection, so a burst of
# GitHub API traffic can be attributed to a caller.
#
# Something authenticating as the `agentydragon` GitHub account drains the entire
# 5000-point GraphQL budget within three minutes of every hourly reset, then keeps
# calling through the 403s. Cilium Hubble cannot see it: with `enable-host-firewall`
# false there is no host endpoint, so host-namespace egress produces no flows at all
# — verified, not assumed. A kernel probe has neither limitation, and sees container
# processes on the node as readily as host ones.
# See <debug/github_rate_limit_monitoring_blind_spot.md>.
#
# CLEANUP(added 2026-09-04): remove this module and its host opt-ins once that note
# names the consumer.
#
# Two deliberate choices, both from failures during the investigation:
#
# - Every outbound connection to a public address is recorded, rather than only
#   GitHub's ranges. The cluster and this host resolve api.github.com into
#   entirely different ranges (140.82.116.0/24 vs Azure 172.182.252.0/22), and an
#   allowlist that is slightly wrong records nothing while looking like a clean
#   result. Private ranges are excluded, which is safe in the other direction: a
#   wrong denylist only over-records. These hosts are Kubernetes nodes, where
#   kubelet probes and pod-to-pod traffic would otherwise contribute on the order
#   of ten records a second (wyrm2: 75 pods, 102 HTTP/TCP probes, 10.6
#   connections/s by their configured periods).
# - The log stays on the host. It is a complete record of every peer this machine
#   dials, and `promtail-journal` ships the journal to Loki, so writing there would
#   put that record in cluster storage.
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
in
{
  options.ducktape.githubApiRecorder = {
    enable = lib.mkEnableOption "outbound TCP connection recording for GitHub API attribution";

    retentionDays = lib.mkOption {
      type = lib.types.int;
      default = 14;
      description = "Rotated daily logs to keep. The investigation needs a few days of overlap with a burn.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      "d ${logDir} 0750 root root -"
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

    # copytruncate: systemd holds the append fd open for the service's lifetime, so
    # renaming the file would leave bpftrace writing to an unlinked inode.
    services.logrotate.settings.github-api-recorder = {
      files = logFile;
      frequency = "daily";
      rotate = cfg.retentionDays;
      compress = true;
      missingok = true;
      notifempty = true;
      copytruncate = true;
    };
  };
}
