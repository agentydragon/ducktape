# Tools leak one top-level /tmp dir per run (tens of thousands of them on wyrm2). tmpfiles can't
# age a glob's top-level dirs separately from the /tmp rule, so reap them after a day. Some
# claude-empty-* dirs are root-owned, hence a system unit.
{ lib, pkgs, ... }:
{
  systemd.services.tmp-leak-reaper = {
    description = "Remove leaked tool temp dirs from /tmp";
    startAt = "daily";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${lib.getExe pkgs.bash} ${./tmp-leak-reaper.sh}";
      Nice = 19;
      IOSchedulingClass = "idle";
    };
  };
  systemd.timers.tmp-leak-reaper.timerConfig = {
    Persistent = true;
    RandomizedDelaySec = "1h";
  };
}
