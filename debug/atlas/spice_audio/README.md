# SPICE Audio Choppiness Investigation

## Problem

Audio over SPICE on wyrm2 has choppiness ranging from slight (~1 glitch every
10-60s) to severe (continuous stuttering). `pw-top` shows xruns (ERR column)
accumulating on the ALSA sink.

## Root Cause (2026-04-17)

The choppiness is **CPU contention from gnome-shell software rendering**, not
PipeWire buffer sizing. When the 2x RTX 5090 GPUs lock up (guest-side
`GPU_IN_FULLCHIP_RESET`, see `wyrm_gpu_lockup.md`), gnome-shell/Mutter falls
back to `kms_swrast` (llvmpipe) — 16 threads of CPU software OpenGL using
~380% CPU. PipeWire runs at normal priority (`SCHED_OTHER`, nice 0) and gets
preempted, causing xruns even at large quantum values.

Evidence:

- gnome-shell at 380% CPU across 16 `llvmpipe-*` threads
- Mutter log: `falling back to kms_swrast`
- System load average ~20-26 on 32 vCPUs
- 5-6% steal time from Proxmox hypervisor
- Fullscreening a static image (less compositor work) eliminates choppiness
- With GPUs healthy, gnome-shell uses GPU 0 for compositing (~197 MiB VRAM)
  and CPU usage is normal

### Why GPUs aren't helping

The VM's display is `vga: virtio,memory=256` (virtio-gpu, **no virgl 3D**).
Two RTX 5090s are passed through via VFIO but are currently locked up
(`nvidia-smi` shows ERR). Mutter creates GBM renderers for all three DRM
cards (virtio-gpu card1, nvidia card0/card2) but falls back to `kms_swrast`
because:

- NVIDIA GPUs are in FULLCHIP_RESET state
- virtio-gpu has no virgl (3D) enabled (`vga: virtio`, not `virtio-gl`)

### Additional factor: Chrome pulls quantum down

Google Chrome requests `node.latency=1024/48000`, which pulls PipeWire's
quantum from the configured 2048 down to 1024 when `min-quantum` allows it.
At quantum=1024 with llvmpipe load, xruns are ~3/second.

## Fix Options

1. **Reboot wyrm2** to recover locked GPUs. gnome-shell will use GPU 0 for
   compositing, eliminating llvmpipe CPU load. This is a temporary fix — GPUs
   lock up again after extended uptime.

2. **Switch to `vga: virtio-gl`** in Proxmox VM config. This enables virgl
   (3D via host AMD iGPU), so Mutter composites with GPU acceleration even when
   NVIDIA GPUs are locked up. Tested and working on wyrm (VM 100) — see
   `../spice_lag/README.md`. Requires VM reboot.

3. **Fix `nvidia-drm.modeset` conflict** in `nix/nixos/hosts/wyrm2/default.nix`:
   `boot.kernelParams` sets `modeset=0` but `hardware.nvidia.modesetting.enable
= true` adds `modeset=1` (last wins). The FLR workaround is currently
   defeated, which may contribute to the GPU lockups.

4. **Enable PipeWire realtime scheduling** (rtkit) as defense-in-depth so
   PipeWire gets `SCHED_RR` priority even under high CPU load.

## Setup

- VM: wyrm2 (VM 110, Proxmox q35, NixOS, GNOME 49 Wayland)
- Display: `vga: virtio,memory=256` (no virgl)
- GPUs: 2x RTX 5090 via VFIO (currently locked up)
- Audio device: `ich9-intel-hda` with SPICE driver
- Audio stack: PipeWire 1.4.9 + pipewire-pulse + WirePlumber
- PipeWire config: `default.clock.quantum=2048`, `default.clock.min-quantum=2048`
  (in `nix/nixos/hosts/wyrm2/default.nix`)
- Note: Two HDA cards present — card 0 is the q35 chipset built-in (no codecs,
  unused), card 1 is the SPICE audio device

## Results: PipeWire quantum sweep (2026-04-16)

Played continuous audio (browser) while cycling through quantum values, 30s each.
ERR counter is cumulative; deltas show xruns per 30s window.

| Quantum | Latency | Xruns/30s |
| ------- | ------- | --------- |
| 256     | ~5.3ms  | ~2427     |
| 512     | ~10.7ms | 8         |
| 1024    | ~21.3ms | 3         |
| 2048    | ~42.7ms | 0         |
| 4096    | ~85.3ms | 0         |

**Note** (2026-04-17): These results were measured when GPUs were healthy and
gnome-shell was not using llvmpipe. With llvmpipe active (GPUs locked up), even
quantum=2048 shows ~0.4 xruns/sec, and quantum=1024 shows ~3 xruns/sec. The
quantum sweep alone cannot fix choppiness caused by CPU contention.

## Files

- `pw-quantum-test.sh` — test script (cycles quantums, captures full `pw-top` output)
- `results/20260416T132850/` — full `pw-top` output per quantum value
