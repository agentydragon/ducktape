# Foxconn DW5932e/DW5934e WWAN modem setup
#
# Initialization: three phases, each triggered differently.
#
# 1. FCC unlock — FoxFlss (bare): allows the software radio to turn on.
#    Wired via ModemManager's fcc-unlock.d; MM calls the script when it sees
#    "Cannot power-up: software radio switch is OFF" during enable. Confirmed
#    firing 2026-04-30 once dmidecode landed in the foxflss runtime PATH —
#    before that the script was being invoked but failing with
#    "Current platform: do not support FccLock!" and exiting 1, so MM gave up
#    (per upstream contract: failed unlock scripts aren't retried).
#
# 2. RF calibration — FoxFlss -f Check_RF_SSKU: writes RF tuner settings, DPR
#    tables, and NR carrier aggregation configs from the platform-specific .dat
#    file into the modem's non-volatile storage. Required for full signal quality
#    and 5G NR operation. Idempotent: no-ops if data already matches.
#    Triggered by an NM dispatcher script when wwan0 comes up — at that point
#    the bearer is fully established and MM is in steady state, so FoxFlss can
#    access the MBIM device through mbim-proxy without contention.
#    The .dat files are packaged in the foxflss derivation and symlinked to the
#    path FoxFlss hardcodes (/opt/foxconn/data/) via systemd-tmpfiles.
#
# 3. FCC unlock watchdog — safety net. Listens to ModemManager state changes
#    via `mmcli -m any -w`; when the modem stays in {enabling,disabled,failed}
#    with power-state=low for >=12s, runs FoxFlss + restarts MM (cooldown
#    120s). Now that phase 1 actually works, this rarely fires; it remains
#    for the case where MM gives up on the unlock script (per upstream
#    contract, failed scripts aren't retried — a transient FoxFlss failure
#    would otherwise wedge the modem until manual intervention).
#    See debug/rugged/hw/foxflss_wwan.md "Watchdog + dmidecode fix".
#
# Hardware: Foxconn DP25-42843-47 (DW5934e, SDX72) — PCI 105b:e11d
# See: debug/rugged/hw/esim.md
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.foxconnWwan;

  foxflss = pkgs.callPackage ../../../packages/foxflss.nix { };

  # Script run by the NM dispatcher on wwan0 up: FCC unlock warm-up, then
  # RF calibration. Bare FoxFlss first because on Ubuntu, ModemManager runs
  # fcc-unlock.d (bare FoxFlss) before FoxFlss.service, which flushes stale
  # MBIM CIDs and makes Check_RF_SSKU reliable. Here fcc-unlock.d never fires
  # (modem boots with power state: on), so we replicate that warm-up explicitly.
  # Absolute path for `sleep` — `pkgs.writeShellScript` does NOT put coreutils
  # on PATH, so a bare `sleep` exits with "command not found" and the &&-chain
  # fails silently before RF cal runs. Confirmed 2026-05-14 in
  # journal_kernel_thisboot from `modem.sh dump`: every recent boot has been
  # silently skipping RF cal.
  foxflssRfCalRun = pkgs.writeShellScript "foxflss-rf-cal-run" ''
    ${foxflss}/bin/FoxFlss && ${pkgs.coreutils}/bin/sleep 5 && ${foxflss}/bin/FoxFlss -f Check_RF_SSKU
  '';

  # FCC unlock + RF calibration for ModemManager fcc-unlock.d.
  # Called by MM with: <script> <dbus-path> <port1> [<port2> ...]
  #
  # CLEANUP(added 2026-04-30): Drop the closed-source FoxFlss binary once nixpkgs
  #   ships libqmi >= 1.38.0 (currently 1.36.0). Upstream MM's
  #   fcc-unlock.available.d/105b script does the job via
  #   `qmicli --fox-set-fcc-authentication` over the FOX service (0xE3),
  #   which is confirmed working on this SDX72 — qmicli --fox-get-firmware-version
  #   returned FDE2.F0.0.0.1.2.TO.003.062. At that point the foxflss derivation,
  #   its /opt/foxconn/data symlinks, and the dispatcher script all collapse
  #   into a one-liner. Remove this when libqmi 1.38+ lands.
  #   See debug/rugged/hw/modem.md TODO list.
  # Watchdog: see foxflss_watchdog.py and the file's docstring for rationale.
  # Listens to MM state changes and runs FoxFlss when the modem is stuck FCC-locked.
  watchdogScript = pkgs.writers.writePython3 "foxflss-watchdog" {
    flakeIgnore = [ "E501" ];
  } (builtins.readFile ./foxflss_watchdog.py);

  fccUnlockScript = pkgs.writeShellScript "foxconn-dw593xe-fcc-unlock" ''
    [ $# -lt 2 ] && exit 1
    shift  # discard DBus path

    for PORT in "$@"; do
      grep -q MBIM "/sys/class/wwan/$PORT/type" 2>/dev/null && {
        MBIM_PORT=$PORT
        break
      }
      echo "$PORT" | grep -q MBIM && {
        MBIM_PORT=$PORT
        break
      }
    done

    [ -n "$MBIM_PORT" ] || exit 2

    ${foxflss}/bin/FoxFlss
    UNLOCK_RESULT=$?
    if [ $UNLOCK_RESULT -ne 0 ]; then
      echo "Foxconn FCC unlock FAILED" >&2
      exit $UNLOCK_RESULT
    fi

    # RF calibration: write RF tuner/NR-CA/MCFG settings to modem non-volatile
    # storage. Reads /opt/foxconn/data/DW5934e_RF.dat (symlinked via
    # systemd-tmpfiles to the nix store). Idempotent.
    ${foxflss}/bin/FoxFlss -f Check_RF_SSKU
    RF_RESULT=$?
    if [ $RF_RESULT -ne 0 ]; then
      echo "Foxconn RF calibration (Check_RF_SSKU) FAILED: $RF_RESULT" >&2
    fi
    exit $RF_RESULT
  '';
in
{
  options.ducktape.foxconnWwan = {
    enable = lib.mkEnableOption "Foxconn DW5932e/DW5934e WWAN FCC unlock";
  };

  config = lib.mkIf cfg.enable {
    # FCC unlock script for ModemManager
    networking.modemmanager.fccUnlockScripts = [
      {
        id = "105b:e11d";
        path = "${fccUnlockScript}";
      }
    ];

    # Disable runtime-PM autosuspend on the modem PCI function. The MHI PCI
    # driver enables runtime PM with a 2s autosuspend delay after probe, so an
    # idle wwan0 (the normal case while on WiFi) gets sent to M3 every few
    # seconds. M3 is the SAME broken Foxconn firmware path that wedges this
    # device on system suspend, but `--test-low-power-suspend-resume` only
    # covers MM's system sleep/wake handler, NOT kernel runtime PM. A runtime
    # autosuspend cycle dropped the firmware into SYS_ERR; the next runtime
    # resume hit `mhi_pci_recovery_work`, which can't reload the SBL (DW5934e
    # ships `.fw = NULL`), did an FLR, and wedged the modem into PBL with no
    # channels, invisible to MM/GNOME until recovery.
    #
    # This is the userspace equivalent of the driver's `.no_m3 = true` quirk
    # (pci_generic.c gate: `pci_pme_capable(...) && !info->no_m3`): no_m3 skips
    # enabling autosuspend at probe, this pins power/control=on so the enabled
    # autosuspend never fires — identical M0-at-idle outcome. Upstream does NOT
    # set no_m3 for 105b:e11d (verified v6.19/master/7.2-rc2, 2026-07-17; only
    # qcom-qdu100 uses it), so we prevent the wedge here instead of carrying a
    # kernel patch. Standby patch + evidence: debug/rugged/hw/modem_suspend_research.md §"P1".
    #
    # The first mitigation only matched PCI add; that was too early because
    # mhi_pci_probe() later calls pm_runtime_set_autosuspend_delay(..., 2000)
    # and re-allows runtime autosuspend. Keep the direct udev write for add,
    # bind, and change, and also trigger a systemd verifier after wwan0 appears.
    # See debug/rugged/hw/modem_suspend_research.md
    # §"Runtime-PM recurrence after the udev rule".
    services.udev.extraRules = ''
      ACTION=="add|bind|change", SUBSYSTEM=="pci", ATTR{vendor}=="0x105b", ATTR{device}=="0xe11d", TEST=="power/control", ATTR{power/control}="on"
      ACTION=="add", SUBSYSTEM=="net", KERNEL=="wwan0", TAG+="systemd", ENV{SYSTEMD_WANTS}+="foxconn-wwan-disable-runtime-pm.service"
    '';

    systemd.services.foxconn-wwan-disable-runtime-pm = {
      description = "Disable runtime PM for Foxconn DW5934e WWAN";
      after = [ "sys-subsystem-net-devices-wwan0.device" ];
      bindsTo = [ "sys-subsystem-net-devices-wwan0.device" ];
      wantedBy = [ "sys-subsystem-net-devices-wwan0.device" ];
      path = [ pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = false;
      };
      script = ''
        set -eu

        matched=0
        for dev in /sys/bus/pci/devices/*; do
          [ -e "$dev/vendor" ] || continue
          [ "$(cat "$dev/vendor")" = "0x105b" ] || continue
          [ "$(cat "$dev/device")" = "0xe11d" ] || continue

          matched=1
          if [ ! -e "$dev/power/control" ]; then
            echo "$dev has no power/control runtime-PM attribute" >&2
            exit 1
          fi

          echo on > "$dev/power/control"
          actual="$(cat "$dev/power/control")"
          if [ "$actual" != "on" ]; then
            echo "$dev power/control is '$actual', expected 'on'" >&2
            exit 1
          fi

          echo "Pinned runtime PM off for $dev"
        done

        if [ "$matched" -eq 0 ]; then
          echo "Foxconn DW5934e PCI device 105b:e11d not found" >&2
          exit 1
        fi
      '';
    };

    # Google Fi cellular connection profile.
    # IPv6 is temporarily disabled so the IPv4 interface can use MTU 1200 after
    # an IPv4 DF-ping investigation found a ~1256-byte path ceiling and missing
    # ICMP fragmentation feedback. That does NOT prove native Fi IPv6 has the
    # same defect: the modem advertises an ipv4v6 bearer, but the active profile
    # currently requests IPv4 only. Re-enable only after the controlled test in
    # debug/rugged/network.md.
    # IPv4 route-metric 1050: WiFi (metric 600) is preferred when available;
    # cellular is used as failover when WiFi is down.
    networking.networkmanager.ensureProfiles.profiles.google-fi = {
      connection = {
        id = "Google Fi";
        type = "gsm";
        autoconnect = true;
      };
      gsm = {
        apn = "h2g2";
        # MTU 1200: conservative cap retained while the Fi PMTU investigation is
        # revalidated. Earlier DF-ping reports suggested a ~1228-byte ceiling
        # with missing ICMP feedback, but the preserved decisive failure was a
        # Cilium-over-Nebula cellular path; that nested path alone cannot prove
        # the direct Fi IPv4 ceiling. The bearer reports MTU 1436. Run
        # debug/rugged/fi-ipv4-mtu-probe.sh before changing this value, and keep
        # that result separate from the native IPv6 experiment in network.md.
        # gsm.mtu is the correct NM property for cellular interface MTU;
        # ipv4.mtu is ignored for GSM connections (ModemManager owns the bearer).
        mtu = 1200;
      };
      ipv4 = {
        method = "auto";
        route-metric = 1050;
        # Keep Wi-Fi's resolver authoritative while both links are up. Fi's
        # resolver synthesizes DNS64 AAAA records for IPv4-only public hosts;
        # otherwise an application can try that address over Wi-Fi's unrelated
        # IPv6 default route before falling back to IPv4. A higher numeric
        # priority loses to the ordinary Wi-Fi connection, but Fi DNS remains
        # available when Wi-Fi is absent. See debug/rugged/network.md.
        dns-priority = 200;
      };
      ipv6 = {
        # Disabled temporarily: IPv6 interfaces cannot use the 1200-byte GSM
        # MTU because IPv6 requires at least 1280. The prior ~1256-byte result
        # came from IPv4 DF probing, so it is an unresolved hypothesis—not proof
        # that native Fi IPv6 is broken. See debug/rugged/network.md.
        method = "disabled";
      };
    };

    # Run RF calibration when wwan0 comes up, via NM dispatcher.
    # This fires after the bearer is fully established (MM in steady state, not
    # mid-reconnect), which avoids the MBIM contention that a plain
    # After=ModemManager.service oneshot suffers during nixos-rebuild switch.
    # Runs on every connect: boot, reconnect, and resume from suspend.
    # FoxFlss is backgrounded so the dispatcher doesn't stall NM.
    networking.networkmanager.dispatcherScripts = [
      {
        source = pkgs.writeShellScript "foxflss-rf-cal-dispatcher" ''
          [ "$1" = "wwan0" ] || exit 0
          [ "$2" = "up" ] || exit 0

          # Run in background — NM waits for dispatcher scripts to finish
          # and we don't want to delay the connection appearing active.
          # --collect: auto-remove the transient unit after it finishes so
          # the next 'up' event can reuse the foxflss-rf-cal unit name.
          ${pkgs.systemd}/bin/systemd-run \
            --no-block \
            --collect \
            --unit=foxflss-rf-cal \
            --description="Foxconn DW5934e FCC unlock and RF calibration" \
            ${foxflssRfCalRun}
        '';
        type = "basic";
      }
    ];

    # FoxFlss hardcodes /opt/foxconn/data/ for RF calibration data. Symlink the
    # packaged .dat files there via systemd-tmpfiles (NixOS doesn't manage /opt).
    systemd.tmpfiles.rules = [
      "d /opt/foxconn 0755 root root - -"
      "d /opt/foxconn/data 0755 root root - -"
      "L+ /opt/foxconn/data/DW5932e_RF.dat - - - - ${foxflss}/share/foxflss/DW5932e_RF.dat"
      "L+ /opt/foxconn/data/DW5934e_RF.dat - - - - ${foxflss}/share/foxflss/DW5934e_RF.dat"
    ];

    # Apply Foxconn's recommended ModemManager suspend handling. Per
    # foxconn-pc/fii_linux Application/FoxFlss/data/mm-suspend-resume-options.conf,
    # MM ≥ 1.24.2 should run with `--test-low-power-suspend-resume`. With this
    # flag, MM's sleep signal handler transitions the modem to
    # MM_MODEM_POWER_STATE_LOW (via QMI DMS Set Operating Mode = LOW_POWER)
    # BEFORE the kernel's MHI suspend path runs — so the modem leaves AMSS
    # ahead of time and `mhi_pci_suspend` takes its early-exit path
    # (drivers/bus/mhi/host/pci_generic.c:1598-1600) instead of attempting the
    # M3 transition that's been wedging this device on resume.
    # See debug/rugged/hw/modem_suspend_research.md §P0 for the full mechanism.
    # CLEANUP: once MM upstream promotes this to a per-plugin default or
    # renames the flag without `--test-` prefix, update the ExecStart here.
    systemd.services.ModemManager.serviceConfig.ExecStart = [
      "" # clear systemd's list-merge so our value replaces the unit's default
      "${pkgs.modemmanager}/sbin/ModemManager --test-low-power-suspend-resume"
    ];

    # CLEANUP(added 2026-04-30): Try retiring this service after ~1 month of
    #   uneventful operation. It was added before we found the dmidecode
    #   root cause and turned out never to have been the actual unlock
    #   path on any failure we observed — MM's wired fcc-unlock.d does
    #   the work. The watchdog only earns its keep if MM's unlock script
    #   ever fails (per upstream contract MM won't retry). To verify
    #   safe to remove: `journalctl -u foxflss-watchdog --since '30 days
    #   ago' | grep -c stuck` should be 0 across boots, suspends, and
    #   slot switches. If so, drop this service block, the
    #   watchdogScript let-binding above, and foxflss_watchdog.py.
    #   See debug/rugged/hw/modem.md TODO list.
    systemd.services.foxflss-watchdog = {
      description = "Foxconn DW5934e FCC-unlock watchdog";
      wants = [ "ModemManager.service" ];
      after = [ "ModemManager.service" ];
      wantedBy = [ "multi-user.target" ];
      # FoxFlss + mmcli + systemctl on PATH.
      path = [
        foxflss
        pkgs.modemmanager
        pkgs.systemd
      ];
      serviceConfig = {
        Type = "simple";
        Restart = "on-failure";
        RestartSec = 10;
        ExecStart = watchdogScript;
      };
    };
  };
}
