# Make Intel RAPL energy counters readable by the non-root node-exporter.
#
# Linux exposes energy_uj as 0400 root:root on some Intel systems.  The
# node-exporter DaemonSet deliberately runs as a non-root user, so its otherwise
# enabled RAPL collector cannot read those counters.  This module grants only
# the energy files a dedicated group read bit; it does not make the exporter
# root or grant it capabilities.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.raplMetrics;
in
{
  options.ducktape.raplMetrics = {
    enable = lib.mkEnableOption "non-root node-exporter access to Intel RAPL energy counters";

    gid = lib.mkOption {
      type = lib.types.ints.between 1 65535;
      default = 45321;
      description = ''
        Dedicated numeric group for RAPL energy-counter readers. This must match
        the node-exporter pod's runAsGroup and fsGroup in the monitoring
        HelmRelease.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.node-exporter-rapl.gid = cfg.gid;

    # RAPL domains are recreated on driver bind/rebind, which resets their
    # sysfs ownership. Run once after boot and again for every powercap domain.
    services.udev.extraRules = ''
      ACTION=="add|change", SUBSYSTEM=="powercap", KERNEL=="intel-rapl*:*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="rapl-energy-permissions.service"
    '';

    systemd.services.rapl-energy-permissions = {
      description = "Grant node-exporter read access to Intel RAPL energy counters";
      after = [ "systemd-modules-load.service" ];
      wantedBy = [ "multi-user.target" ];
      path = [ pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        PrivateTmp = true;
        ProtectHome = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = [
          "CAP_CHOWN"
          "CAP_FOWNER"
        ];
      };
      script = ''
        set -eu
        shopt -s nullglob

        energy_files=(/sys/class/powercap/intel-rapl*:*/energy_uj)
        if [ "''${#energy_files[@]}" -eq 0 ]; then
          echo "No Intel RAPL energy counters found" >&2
          exit 1
        fi

        for energy_file in "''${energy_files[@]}"; do
          chown root:${toString cfg.gid} "$energy_file"
          chmod 0440 "$energy_file"
        done
      '';
    };
  };
}
