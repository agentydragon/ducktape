# Tools leak one top-level /tmp dir per run (tens of thousands of them on wyrm2). tmpfiles can't
# age a glob's top-level dirs separately from the /tmp rule, so reap them after a day. Some
# claude-empty-* dirs are root-owned, hence a system unit.
{
  systemd.services.tmp-leak-reaper = {
    description = "Remove leaked tool temp dirs from /tmp";
    startAt = "daily";
    serviceConfig = {
      Type = "oneshot";
      Nice = 19;
      IOSchedulingClass = "idle";
    };
    script = ''
      find /tmp -mindepth 1 -maxdepth 1 -type d -mmin +1440 \( \
          -name 'claude-empty-*' -o -name 'cdk8s.outdir.*' -o -name 'scoped_dir*' \
        \) -exec rm -rf --one-file-system {} +
      # Empty only: a nix shell open for over a day keeps a stale-mtime dir in use.
      find /tmp -mindepth 1 -maxdepth 1 -type d -mmin +1440 -empty \( \
          -name 'nix-shell.*' -o -name 'nix-develop-*' \
        \) -delete
    '';
  };
  systemd.timers.tmp-leak-reaper.timerConfig = {
    Persistent = true;
    RandomizedDelaySec = "1h";
  };
}
