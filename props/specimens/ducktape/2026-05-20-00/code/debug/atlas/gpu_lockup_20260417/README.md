# GPU Lockup Capture — 2026-04-17

## Event

Both RTX 5090 GPUs locked up inside wyrm2 (VM 110) at ~2026-04-16 13:04 PDT
(kernel timestamp ~91025s, ~25h after boot on Apr 15 11:47). No preceding
errors — the FULLCHIP_RESET assertions are the very first NVIDIA messages in
this boot's dmesg.

## What We Ruled Out

- **Host-side PCIe/VFIO instability**: Zero SATA errors, zero soft lockups on
  atlas. The host is healthy. PCIe link to both GPUs is Gen 5 x8 (32.0 GT/s)
  post-lockup — the link did not drop. This is not the same failure mode as
  incidents 1-16 in `black_screen_lockup.md`.
- **NVIDIA Runtime D3**: `nvidia-bug-report.sh` confirms `Runtime D3 status:
Disabled by default` on both GPUs. The platform lacks ACPI `_PR3` power
  resources (missing `power_resources_D3hot` sysfs directory). The full D3cold
  runtime PM path is not active.
- **External trigger**: No Xid errors, no RC watchdog timeout, no host-side
  events, nothing unusual in guest journal in the minutes before the lockup.
  Normal k8s pod churn (flux tf-runners, ollama bearer token runner).

## What We Haven't Ruled Out

- **Other GPU power states**: Runtime D3 is disabled, but the GPU has many
  other internal power management mechanisms: P-states (GPUs were in P8/idle),
  video memory power-off (listed as "Supported"), PCIe link-level PM inside the
  GPU, and GSP-RM firmware sleep states on the internal RISC-V core. We have no
  visibility into any of these.
- **`nvidia-drm.modeset=1`**: Active despite `modeset=0` in boot.kernelParams
  (NixOS config conflict — `hardware.nvidia.modesetting.enable = true` adds a
  second `modeset=1` that wins). The `modeset=0` workaround was added to prevent
  GPU issues under VFIO. Unknown whether this contributed.
- **`DynamicPowerManagement: 3`** (FINE): Set in the driver params. Runtime D3
  is disabled (platform doesn't support it), but this parameter may affect other
  GPU-internal PM behavior. We don't know what set it to 3 or what other effects
  it has beyond Runtime D3.
- **GSP-RM firmware crash**: Blackwell runs GPU firmware (GSP-RM) on an internal
  RISC-V core. The firmware has its own state management that's opaque to the
  host driver. A firmware crash would look exactly like this — instant
  FULLCHIP_RESET with no preceding host-visible error. The bug report hung
  before it could dump GPU registers or GSP-RM logs, so we have no data on
  firmware state.
- **Blackwell VFIO bugs**: RTX 5090 VFIO is known buggy (community reports in
  `black_screen_lockup.md`). The open kernel module is the only option for
  Blackwell.

## Key Observations

- **No pre-failure signal**: The very first NVRM message in the entire boot is
  the FULLCHIP_RESET at t=91025. Zero GPU-related kernel messages before that.
- **Video BIOS unreadable**: `??.??.??.??.??` — driver can't read VBIOS from
  the locked GPUs. Previously showed actual versions when healthy.
- **GPU Firmware: N/A** — GSP-RM state unreadable.
- **`modeset:Y` and `fbdev:Y`** confirmed active in bug report module params.
- **Chrome `WebGL1 blocklisted` errors** occurring before and after the lockup
  — Chrome's GPU process was running but WebGL was disabled by policy. These
  are "blocklisted" (refused), not crashes.
- **`gpu_vaspace` errors at t=91025, 93273, 93814, 120113** — repeated failed
  VA space allocations. These are consequences (something keeps retrying the
  dead GPUs), not the cause.
- **GPUs were idle**: No CUDA/Ollama workload, 2 MiB VRAM used on each GPU.

## nvidia-bug-report.sh

Ran but hung (expected with locked GPUs — tries to query them and blocks).
Partial output captured in `nvidia-bug-report.log.gz` (2932 lines). Contains
system info, module parameters, power state info, but no GPU register dumps
or GSP-RM logs (those would have been captured after the point where it hung).

## Next Steps

1. **Set up continuous GPU monitoring** — DONE. Added `ducktape.gpuMonitor`
   NixOS module (`nix/nixos/modules/gpu-monitor.nix`), enabled on wyrm2.
   Systemd service `gpu-monitor-poll` writes nvidia-smi telemetry every 30s
   to `/var/log/gpu-monitor/telemetry-YYYY-MM-DD.csv`. Kernel GPU errors
   (NVRM, Xid) are already in the journal via dmesg.
   Takes effect on next `nixos-rebuild switch` + reboot.
2. **Fix `nvidia-drm.modeset` conflict** — set `modesetting.enable = false` in
   NixOS config. This is a real bug regardless of whether it caused this lockup.
3. **Investigate `DynamicPowerManagement` setting** — determine what's setting
   it to 3 and whether it affects anything beyond the (already disabled)
   Runtime D3.
4. **Try `nvidia-bug-report.sh --safe-mode`** next time — the script suggests
   this flag when it hangs, may skip the queries that block on dead GPUs.

## Files

- `nvidia-bug-report.log.gz` — partial nvidia-bug-report (hung on GPU queries)
- `nvidia-bug-report.log` — decompressed version
- `nvidia-smi.txt` — nvidia-smi output (shows ERR)
- `nvidia-smi-q.txt` — detailed nvidia-smi query
- `nvidia-smi-query.txt` — CSV query (all N/A due to lockup)
- `nvidia-gpu-info.txt` — /proc/driver/nvidia/gpus/ info
- `nvidia-params.txt` — nvidia module parameters
- `nvidia-version.txt` — driver version (580.142 Open)
- `pci-gpu-info.txt` — PCI device state (link speed, power, d3cold)
- `dmesg-nvidia.txt` — all NVRM/nvidia lines from dmesg
- `dmesg-lockup-window.txt` — dmesg around t=91025
- `dmesg-full.txt` — complete dmesg for the boot
- `journal-lockup-window.txt` — systemd journal 12:55-13:15
- `journal-gnome-renderer.txt` — gnome-shell EGL/renderer messages
- `kernel-cmdline.txt` — kernel cmdline (shows modeset conflict)
- `drm-info.txt` — DRM card info

## Related

- <../wyrm_gpu_lockup.md> — prior GPU lockup investigation (Feb 2026)
- <../black_screen_lockup.md> — host-level chipset/VFIO instability (resolved)
- <../gpu-strategy.md> — GPU strategy overview
