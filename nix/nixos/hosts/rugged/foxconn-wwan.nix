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
  # CLEANUP(2026-04-30): Drop the closed-source FoxFlss binary once nixpkgs
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

    # Disable runtime-PM autosuspend on the modem PCI function. The MHI pci
    # driver enables runtime PM with a 2s autosuspend delay by default, so an
    # idle wwan0 (the normal case while on WiFi) gets sent to M3 every few
    # seconds. M3 is the SAME broken Foxconn firmware path that wedges this
    # device on system suspend — but `--test-low-power-suspend-resume` only
    # covers MM's system sleep/wake handler, NOT kernel runtime PM. A runtime
    # autosuspend cycle dropped the firmware into SYS_ERR; the next runtime
    # resume hit `mhi_pci_recovery_work`, which can't reload the SBL (DW5934e
    # ships `.fw = NULL`), did an FLR, and wedged the modem into PBL with no
    # channels — invisible to MM/GNOME until reboot.
    #   Live trace: 2026-06-05 02:30:48 `mhi mhi0: Resuming from non M3 state
    #   (SYS ERROR)` with NO `PM: suspend entry` nearby (nearest system sleep
    #   was 20min later) — i.e. a runtime-PM resume, not a system resume.
    # Keeping power/control=on pins the modem at M0 during normal operation;
    # M3 only happens during real system suspend, where the MM flag handles it.
    # See debug/rugged/hw/modem_suspend_research.md §"Runtime-PM autosuspend".
    services.udev.extraRules = ''
      ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x105b", ATTR{device}=="0xe11d", ATTR{power/control}="on"
    '';

    # Google Fi cellular connection profile.
    # IPv6 never-default: many WiFi networks only provide ULA IPv6 (no default
    # route). Cellular provides global IPv6 with a default route, which causes
    # all IPv6 traffic to silently route over cellular — breaking apps that
    # can't traverse carrier NAT. Disabling the IPv6 default is conservative
    # but safe on all networks. TODO: revisit if true IPv6 failover is needed.
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
        # MTU 1200: outgoing path MTU on Google Fi is ~1228 bytes (confirmed by
        # DF-bit ping probing) and ICMP Fragmentation Needed is suppressed, so
        # PMTU discovery never fires. Bearer-reported MTU is 1436 but the actual
        # path drops packets silently above ~1228B. With MTU 1200, TCP MSS=1160
        # and max IP packet=1200B, safely under the path limit.
        # gsm.mtu is the correct NM property for cellular interface MTU;
        # ipv4.mtu is ignored for GSM connections (ModemManager owns the bearer).
        mtu = 1200;
      };
      ipv4 = {
        method = "auto";
        route-metric = 1050;
      };
      ipv6 = {
        # Disabled: Linux enforces a 1280-byte minimum MTU on IPv6-enabled
        # interfaces (RFC 2460). With ipv4v6 bearer, this overrides gsm.mtu=1200
        # and pins the interface at 1280, which exceeds the ~1256B path MTU
        # ceiling on Google Fi (confirmed by DF-bit probing). Disabling IPv6
        # removes the floor and lets gsm.mtu=1200 actually take effect.
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

    # CLEANUP(2026-04-30): Try retiring this service after ~1 month of
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
