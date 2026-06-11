# GPU health monitoring for VFIO-passthrough NVIDIA GPUs.
#
# Collects periodic GPU telemetry (temperature, power, P-state, PCIe link,
# clocks, ECC errors) to /var/log/gpu-monitor/ as timestamped CSV.
# Kernel GPU errors (NVRM, Xid) are already in the journal via dmesg.
#
# Purpose: provide pre-failure telemetry for guest-side GPU lockups that
# currently have zero visibility. See debug/atlas/gpu_lockup_20260417/README.md.
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.ducktape.gpuMonitor;
  logDir = "/var/log/gpu-monitor";
in
{
  options.ducktape.gpuMonitor = {
    enable = lib.mkEnableOption "GPU health monitoring";

    intervalSec = lib.mkOption {
      type = lib.types.int;
      default = 30;
      description = "Polling interval in seconds for nvidia-smi telemetry.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      "d ${logDir} 0755 root root -"
    ];

    # Periodic GPU telemetry collection.
    # Appends one CSV row per GPU per interval to a daily log file.
    # On nvidia-smi failure (GPU locked up), logs the failure timestamp
    # and stops polling (no point hammering a dead GPU).
    systemd.services.gpu-monitor-poll = {
      description = "GPU telemetry poller";
      after = [ "multi-user.target" ];
      wantedBy = [ "multi-user.target" ];
      path = [ config.hardware.nvidia.package ];
      serviceConfig = {
        Type = "simple";
        Restart = "on-failure";
        RestartSec = 60;
      };
      script = ''
        header="timestamp,gpu_index,gpu_name,pstate,temperature_gpu,power_draw_w,pcie_gen,pcie_width,clocks_sm_mhz,clocks_mem_mhz,memory_used_mib,memory_total_mib,ecc_uncorrected"
        query="pstate,temperature.gpu,power.draw,pcie.link.gen.current,pcie.link.width.current,clocks.current.sm,clocks.current.memory,memory.used,memory.total,ecc.errors.uncorrected.volatile.total"

        echo "gpu-monitor: starting telemetry polling every ${toString cfg.intervalSec}s"

        while true; do
          date_tag=$(date +%Y-%m-%d)
          log_file="${logDir}/telemetry-''${date_tag}.csv"

          # Write header if file is new
          if [ ! -f "$log_file" ]; then
            echo "$header" > "$log_file"
          fi

          ts=$(date -Iseconds)

          # Try nvidia-smi. If it fails or hangs, the GPU is locked up.
          if output=$(timeout 10 nvidia-smi \
              --query-gpu=index,name,"$query" \
              --format=csv,noheader,nounits 2>&1); then
            echo "$output" | while IFS= read -r line; do
              echo "''${ts},''${line}" >> "$log_file"
            done
          else
            echo "''${ts},NVIDIA-SMI FAILED: $output" >> "$log_file"
            echo "gpu-monitor: nvidia-smi failed at $ts — GPU likely locked up. Stopping poll." >&2
            # Log the event and exit. systemd will restart after RestartSec.
            # If the GPU stays dead, nvidia-smi will keep failing and the
            # restart loop provides periodic "still dead" markers.
            exit 1
          fi

          sleep ${toString cfg.intervalSec}
        done
      '';
    };

  };
}
