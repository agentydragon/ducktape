# Tags suspend/resume transitions in the journal with the battery percentage
# at each end, so a failure to actually suspend (screen locks but the CPU
# keeps running) shows up as a battery drop with no matching suspend/resume
# pair, rather than just "the battery is somehow low again". Loki already
# ships the journal cluster-wide (cluster/k8s/monitoring/loki/promtail-journal-helmrelease.yaml),
# so this needs no new shipping path -- just something worth grepping for.
#
# Mechanism: systemd invokes every executable in /etc/systemd/system-sleep/
# around suspend/hibernate with $1=pre|post, $2=suspend|hibernate|hybrid-sleep.
# This is the standard systemd hook point (no NixOS-specific wrapper exists
# for it), unrelated to powerManagement.resumeCommands, which only covers the
# post-resume half.
{ pkgs, ... }:
let
  batteryPercent = pkgs.writeShellScript "rugged-battery-percent" ''
    cat /sys/class/power_supply/BAT0/capacity 2>/dev/null || echo NA
  '';
  logSuspendState = pkgs.writeShellScript "rugged-log-suspend-state" ''
    set -euo pipefail
    phase="$1" # pre | post
    kind="$2"  # suspend | hibernate | hybrid-sleep
    battery="$(${batteryPercent})"
    ${pkgs.util-linux}/bin/logger -t rugged-power "sleep-''${phase} kind=''${kind} battery=''${battery}%"
  '';
in
{
  environment.etc."systemd/system-sleep/rugged-battery-log" = {
    source = logSuspendState;
    mode = "0755";
  };
}
