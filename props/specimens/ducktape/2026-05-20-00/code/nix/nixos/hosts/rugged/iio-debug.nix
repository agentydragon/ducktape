# IIO sensor proxy wedge instrumentation
#
# Captures observable state across suspend cycles, watchdog probes for the
# wedge condition every 60s, and turns on verbose userspace + kernel debug
# output for the relevant code paths. Does NOT auto-recover — the wedge
# must remain observable for the next forensic capture.
#
# See debug/rugged/auto_rotate.md "Investigation log" for the components,
# hypotheses, and instrumentation menu this module is implementing.
#
# Output locations:
# - /var/log/iio-watchdog.log      one line per 60s probe
# - /var/log/iio-bracket/          systemd-sleep pre/post snapshots
# - /var/log/iio-wedge/<ts>/       full state dump on detected wedge
# - journalctl -u iio-sensor-proxy verbose (G_MESSAGES_DEBUG=all)
# - dmesg                          kernel pr_debug for hid-sensor / iio / ish modules
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.iioDebug;

  # Modules whose pr_debug() output we want in dmesg. +pmf =
  # print, prepend module name, prepend function name.
  dyndbgModules = [
    "hid_sensor_trigger"
    "hid_sensor_iio_common"
    "hid_sensor_hub"
    "hid_sensor_accel_3d"
    "hid_sensor_als"
    "industrialio"
    "intel_ishtp_hid"
    "intel_ish_ipc"
  ];

  # Common state snapshot. Writes to $1 (or stdout if unset).
  snapshotScript = pkgs.writeShellScript "iio-debug-snapshot" ''
    set -u
    out="''${1:-/dev/stdout}"
    cat=${pkgs.coreutils}/bin/cat
    ls=${pkgs.coreutils}/bin/ls
    busctl=${pkgs.systemd}/bin/busctl
    pgrep=${pkgs.procps}/bin/pgrep
    {
      echo "===timestamp==="
      ${pkgs.coreutils}/bin/date -Iseconds

      echo "===iio devices==="
      for d in /sys/bus/iio/devices/iio:device*; do
        name=$($cat "$d/name" 2>/dev/null || echo unknown)
        echo "--- $d ($name) ---"
        for f in buffer/enable buffer/length buffer/watermark buffer/data_available \
                 trigger/current_trigger \
                 in_accel_x_raw in_accel_y_raw in_accel_z_raw \
                 in_intensity_both_raw in_illuminance_raw \
                 in_accel_sampling_frequency in_intensity_sampling_frequency \
                 power/runtime_status power/runtime_active_time \
                 power/runtime_suspended_time power/control; do
          if [ -e "$d/$f" ]; then
            v=$($cat "$d/$f" 2>&1)
            echo "  $f = $v"
          fi
        done
        if [ -d "$d/scan_elements" ]; then
          echo "  scan_elements:"
          for f in "$d/scan_elements/"*_en; do
            [ -e "$f" ] && echo "    $(${pkgs.coreutils}/bin/basename "$f") = $($cat "$f" 2>&1)"
          done
        fi
      done

      echo "===iio triggers==="
      for t in /sys/bus/iio/devices/trigger*; do
        [ -d "$t" ] && echo "$(${pkgs.coreutils}/bin/basename "$t") name=$($cat "$t/name" 2>/dev/null)"
      done

      echo "===iio-sensor-proxy daemon==="
      pid=$($pgrep -ox iio-sensor-prox 2>/dev/null || true)
      if [ -n "$pid" ]; then
        echo "pid=$pid"
        echo "comm=$($cat /proc/$pid/comm 2>/dev/null)"
        echo "started_at=$(${pkgs.coreutils}/bin/stat -c %Y /proc/$pid 2>/dev/null)"
        echo "--- threads ---"
        for t in /proc/$pid/task/*; do
          tid=$(${pkgs.coreutils}/bin/basename "$t")
          tcomm=$($cat "$t/comm" 2>/dev/null || echo "?")
          tw=$($cat "$t/wchan" 2>/dev/null || echo "?")
          tsy=$($cat "$t/syscall" 2>/dev/null | ${pkgs.coreutils}/bin/head -c 120 || echo "?")
          echo "TID $tid: comm=$tcomm wchan=$tw syscall=$tsy"
          echo "  stack:"
          $cat "$t/stack" 2>/dev/null | ${pkgs.gnused}/bin/sed 's/^/    /' || echo "    (unavailable)"
        done
        echo "--- fd table ---"
        $ls -la "/proc/$pid/fd/" 2>/dev/null
      else
        echo "iio-sensor-proxy not running"
      fi

      echo "===busctl props==="
      for prop in HasAccelerometer AccelerometerOrientation HasAmbientLight LightLevel; do
        ${pkgs.coreutils}/bin/printf '%s = ' "$prop"
        $busctl --system --no-pager get-property net.hadess.SensorProxy /net/hadess/SensorProxy net.hadess.SensorProxy "$prop" 2>&1 \
          | ${pkgs.coreutils}/bin/head -c 200
        echo
      done
    } >> "$out"
  '';

  # systemd-sleep hook: invoked with $1=pre|post, $2=suspend|hibernate|...
  bracketScript = pkgs.writeShellScript "iio-debug-bracket" ''
    set -u
    phase="$1"
    when="$2"
    ts=$(${pkgs.coreutils}/bin/date -u +%Y-%m-%dT%H-%M-%S)
    logdir=/var/log/iio-bracket
    ${pkgs.coreutils}/bin/mkdir -p "$logdir"
    logfile="$logdir/$ts-$phase-$when.log"
    {
      echo "===iio-debug-bracket phase=$phase when=$when==="
      echo "boot_id=$(${pkgs.coreutils}/bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
      ${snapshotScript} /dev/stdout
      echo "===dmesg tail==="
      ${pkgs.util-linux}/bin/dmesg --time-format iso 2>/dev/null | ${pkgs.coreutils}/bin/tail -n 50
      echo "===iio-sensor-proxy journal tail==="
      ${pkgs.systemd}/bin/journalctl -u iio-sensor-proxy.service -n 50 -o short-iso --no-pager 2>/dev/null
    } >> "$logfile" 2>&1
  '';

  # Watchdog probe — every 60s. Detects the wedge by analyzing the
  # iio-sensor-proxy journal: in buffered mode, the daemon prints
  # "Accel read from IIO ..." (g_debug) on every successful read and
  # "No new data available on '...'" on every EAGAIN. Both require
  # G_MESSAGES_DEBUG=all (set on the daemon unit below).
  watchdogScript = pkgs.writeShellScript "iio-debug-watchdog" ''
    set -u
    log=/var/log/iio-watchdog.log
    journalctl=${pkgs.systemd}/bin/journalctl
    grep=${pkgs.gnugrep}/bin/grep
    cat=${pkgs.coreutils}/bin/cat
    ts=$(${pkgs.coreutils}/bin/date -Iseconds)

    # Locate accel_3d
    accel_dev=
    for d in /sys/bus/iio/devices/iio:device*; do
      [ "$($cat "$d/name" 2>/dev/null)" = "accel_3d" ] && accel_dev=$d && break
    done
    if [ -z "$accel_dev" ]; then
      echo "$ts ERR no_accel_device" >> "$log"
      exit 0
    fi

    buffer_enable=$($cat "$accel_dev/buffer/enable" 2>/dev/null || echo "?")
    current_trigger=$($cat "$accel_dev/trigger/current_trigger" 2>/dev/null || echo "")
    if [ -n "$current_trigger" ]; then mode=buffered; else mode=polled; fi

    # Count daemon log lines in the last 60s. If the daemon was restarted
    # mid-window or G_MESSAGES_DEBUG isn't set, both counts will be 0 and
    # we report idle, which is still useful.
    jbuf=$($journalctl -u iio-sensor-proxy.service --since "60 sec ago" --no-pager 2>/dev/null || true)
    n_ok=$(printf '%s\n' "$jbuf" | $grep -c 'Accel read from IIO' || true)
    n_eagain=$(printf '%s\n' "$jbuf" | $grep -c 'No new data available' || true)

    state=UNKNOWN
    if [ "$n_ok" -gt 0 ] && [ "$n_eagain" -eq 0 ]; then
      state=ok
    elif [ "$n_ok" -gt 0 ] && [ "$n_eagain" -gt 0 ]; then
      state=degraded
    elif [ "$n_ok" -eq 0 ] && [ "$n_eagain" -gt 30 ]; then
      state=WEDGE
    elif [ "$n_ok" -eq 0 ] && [ "$n_eagain" -eq 0 ]; then
      state=idle
    fi

    echo "$ts state=$state mode=$mode buffer_enable=$buffer_enable n_ok=$n_ok n_eagain=$n_eagain trigger=$current_trigger" >> "$log"

    if [ "$state" = "WEDGE" ]; then
      snapdir="/var/log/iio-wedge/$ts"
      ${pkgs.coreutils}/bin/mkdir -p "$snapdir"
      ${snapshotScript} "$snapdir/snapshot.log"
      ${pkgs.util-linux}/bin/dmesg --time-format iso 2>/dev/null > "$snapdir/dmesg.log"
      $journalctl -u iio-sensor-proxy.service --since "10 min ago" -o short-iso --no-pager > "$snapdir/iio-sensor-proxy.journal" 2>&1
      $journalctl --user-unit org.gnome.Shell --since "10 min ago" -o short-iso --no-pager > "$snapdir/gnome-shell.journal" 2>&1 || true
    fi
  '';

  # Apply dyndbg flags at boot (in case modules were already loaded before
  # /etc/modprobe.d/ was consulted, e.g. from an early udev pass).
  dyndbgApplyScript = pkgs.writeShellScript "iio-debug-dyndbg-apply" ''
    set -u
    ctl=/sys/kernel/debug/dynamic_debug/control
    [ -w "$ctl" ] || exit 0
    ${lib.concatMapStrings (m: ''
      ${pkgs.coreutils}/bin/echo "module ${m} +pmf" > "$ctl" 2>/dev/null || true
    '') dyndbgModules}
  '';
in
{
  options.ducktape.iioDebug = {
    enable = lib.mkEnableOption "iio sensor proxy wedge instrumentation";
  };

  config = lib.mkIf cfg.enable {
    # 1. Modprobe options for dynamic debug — applied when the modules
    # are loaded by udev / modprobe.
    boot.extraModprobeConfig = lib.concatMapStrings (m: ''
      options ${m} dyndbg=+pmf
    '') dyndbgModules;

    # 2. Re-apply dyndbg after debugfs is mounted, for modules that were
    # already loaded before modprobe.d was consulted.
    systemd.services.iio-debug-dyndbg = {
      description = "Apply dyndbg flags for iio-sensor wedge investigation";
      wantedBy = [ "multi-user.target" ];
      after = [ "sys-kernel-debug.mount" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${dyndbgApplyScript}";
      };
    };

    # 3. systemd-sleep hook bracket script.
    environment.etc."systemd/system-sleep/iio-bracket" = {
      source = bracketScript;
      mode = "0755";
    };

    # 4. Verbose g_debug output from the daemon.
    systemd.services.iio-sensor-proxy.environment.G_MESSAGES_DEBUG = "all";

    # 5. Watchdog probe — every 60s.
    systemd.services.iio-debug-watchdog = {
      description = "Probe iio sensor state for the wedge condition";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${watchdogScript}";
      };
    };
    systemd.timers.iio-debug-watchdog = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "60s";
        Unit = "iio-debug-watchdog.service";
        AccuracySec = "5s";
      };
    };

    # 6. Pre-create log directories.
    systemd.tmpfiles.rules = [
      "d /var/log/iio-bracket  0755 root root -"
      "d /var/log/iio-wedge    0755 root root -"
      "f /var/log/iio-watchdog.log 0644 root root -"
    ];
  };
}
