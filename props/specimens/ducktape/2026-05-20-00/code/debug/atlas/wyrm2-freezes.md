# wyrm2 (VM 110) UI Freezes — Investigation 2026-03-19

## Symptom

wyrm2 desktop UI unresponsive ~80% of the time. Recurring. Atlas host itself
remains responsive. VM eventually appeared to crash but recovered.

## Background: virtual display adapters in QEMU

The VM has two kinds of GPUs: passthrough NVIDIA RTX 5090s (physical hardware
accessed via VFIO) and a **virtual display adapter** that QEMU emulates for
SPICE remote access. These are completely independent — the virtual adapter
provides a framebuffer for the SPICE client, the passthrough GPUs are for
compute and local rendering. The virtual adapter choice is what's broken.

**QXL** (`vga: qxl`): Paravirtual GPU designed for the SPICE remote display
protocol (Red Hat/Qumranet). QEMU emulates a PCI graphics card; the guest loads
the `qxl` kernel DRM driver with its own TTM implementation (`qxl_ttm.c`) for
managing its small video RAM (default 16 MiB). Optimized for 2D/remote desktop.

**VirtIO-GPU** (`vga: virtio`): Paravirtual GPU using the standard VirtIO I/O
framework. More modern, actively maintained. Guest uses the `virtio-gpu` kernel
driver. Works with both SPICE and VNC. Can optionally support 3D acceleration
via virgl. Does not use the broken QXL TTM code path.

## Root Cause: QXL TTM bug

`[TTM] Buffer eviction failed` is a **known QXL virtual GPU driver bug**, not
an NVIDIA issue. The VM has `vga: qxl`, and the QXL kernel driver's TTM
(Translation Table Manager) subsystem fails to manage its video memory buffers,
spamming the error every ~17 seconds and freezing the graphics console.

This is a known issue on Linux kernels 6.8+ with QXL under KVM/QEMU:

- https://forum.proxmox.com/threads/ttm-buffer-eviction-failed.152720/
- https://access.redhat.com/solutions/7129359 (subscriber-only; title:
  "shutdown or reboot hang with '[TTM] Buffer eviction failed' in
  qxl_fence_wait()")
- https://forums.truenas.com/t/kernel-ttm-buffer-eviction-failed/7345

The error chain is: `[TTM] Buffer eviction failed` → `qxl object_init failed`
→ `[drm:qxl_gem_object_create] *ERROR* Failed to allocate GEM object`. This is
the QXL driver's TTM, not NVIDIA's.

### Why gnome-shell is on NVIDIA (and that's fine)

nvidia-smi shows gnome-shell using 197 MiB on GPU 0. This is expected — with
`nvidia-drm.modeset=1` the NVIDIA GPUs register as DRM devices and the
compositor prefers them over QXL. The NVIDIA GPUs are perfectly capable of
running gnome-shell. The freezes come from QXL's broken TTM, not from NVIDIA.

### Causation chain

1. QXL driver loaded (VM has `vga: qxl`)
2. QXL's TTM subsystem hits a bug managing its video memory buffers
3. `[TTM] Buffer eviction failed` fires every ~17 seconds
4. QXL graphics pipeline stalls → UI freezes
5. NVIDIA GPUs are unrelated (no Xid errors, no DRM errors, VRAM nearly empty)

## Fix Applied

**Switched from QXL to VirtIO-GPU** (`vga: virtio`) in
`terraform/nixos-dev-env/main.tf:150`. Eliminates the broken QXL TTM entirely.

**Initial problem**: Proxmox console showed "Display output not active" with
virtio-gpu. Root cause: `max_hostmem=16MB` (Proxmox default `memory=16`) was
too small — QEMU rejected guest display operations with
`VIRTIO_GPU_RESP_ERR_INVALID_RESOURCE_ID` (0x1203) for `SET_SCANOUT`,
`RESOURCE_FLUSH`, and `TRANSFER_TO_HOST_2D` as resources were discarded under
memory pressure. Fixed by setting `vga: virtio,memory=256` (256MB). Proxmox
noVNC console now works with smooth composited desktop rendering.

## QXL TTM Bug — Upstream Fix Status (updated 2026-03-20)

The root cause commit `5a838e5d5825` ("drm/qxl: simplify qxl_fence_wait") had
a messy upstream history:

1. Reverted in kernel 6.8.7 (`07ed11afb68d`)
2. **Reapplied** in 6.8.10
3. Reverted again; fix confirmed in **kernel 6.14+**

wyrm2 is on kernel 6.17, so **the QXL TTM bug should be fixed**. Switching back
to QXL is an option if Proxmox console access is needed. QXL as a project is
essentially unmaintained (no active development), but the kernel driver receives
bug fixes.

Sources:

- https://access.redhat.com/solutions/7129359
- https://bugs.launchpad.net/bugs/2065153
- https://lists.ubuntu.com/archives/kernel-team/2025-July/161302.html

### Other alternatives considered

- Increase QXL VRAM with `vgamem: 65536` — limited success in reports.
- `vga: none` — headless, loses all virtual console access.
- `vga: std` — basic VGA, VNC works, low resolution/no acceleration.

## PCIe Link Speed (separate issue, not related to freezes)

Both GPUs show PCIe Gen 1 x8 in nvidia-smi. This is **normal idle behavior** —
NVIDIA GPUs dynamically downshift PCIe link speed to Gen 1 when in P8 (idle)
power state to save power. This is independent of OS-level ASPM (which is
disabled via `pcie_aspm=off`). Under GPU load, the link should ramp up to
Gen 5.

Both GPUs were in P8 state during diagnostics (GPU 0: 41W/575W, GPU 1:
12W/600W, 0% utilization).

### PCIe topology (for reference)

Root ports cap at x8 due to CPU lane bifurcation:

```
CPU (Raphael/Granite Ridge)
  └── 00:01.0 (Dummy Host Bridge)
       ├── 00:01.1 ──[x8]── 01:00.0 GPU 0 (RTX 5090 ZOTAC)
       ├── 00:01.2 ──[x4?]── 02:00.0 NVMe (Phison PS5026-E26)
       └── 00:01.3 ──[x8]── 03:00.0 GPU 1 (RTX 5090 Gigabyte)
```

Host `lspci` confirms root ports cap at x8, ASPM disabled, target speed 32 GT/s:

| Component           | LnkCap    | LnkCtl                        | LnkSta             |
| ------------------- | --------- | ----------------------------- | ------------------ |
| Root port `00:01.1` | Gen 5, x8 | ASPM Disabled, Target 32 GT/s | 2.5 GT/s x8 (idle) |
| Root port `00:01.3` | Gen 5, x8 | ASPM Disabled, Target 32 GT/s | 2.5 GT/s x8 (idle) |

### Host kernel cmdline

Verified correct:

```
root=ZFS=rpool/ROOT/pve-1 boot=zfs amd_iommu=on iommu=pt pcie_aspm=off ahci.mobile_lpm_policy=1
```

## Evidence from Guest Diagnostics

### Second dump (22:54 PDT)

**TTM errors continuous:**

- `[TTM] Buffer eviction failed` every ~17s, unbroken from 22:21 through 22:50+
- Only error in dmesg — no Xid errors, no DRM errors, no GPU resets

**NVIDIA GPUs healthy:**

- GPU 0: 334 MiB / 32607 MiB VRAM, P8 state, 30C
- GPU 1: 21 MiB / 32607 MiB VRAM, P8 state, 51C
- Driver: NVIDIA 580.119.02 Open Kernel Module
- Addressing Mode: HMM

**Guest health fine:**

- Memory: 94 GiB total, 34 GiB free, no swap
- PSI: all zeros (memory, I/O, CPU)
- Load: 0.56 (normal for 32 vCPUs)
- No D-state processes, no failed systemd units

**Guest kernel cmdline issues (minor):**

```
nvidia-drm.modeset=0 ... nvidia-drm.modeset=1   ← contradictory (last wins → 1)
nvidia.NVreg_OpenRmEnableUnsupportedGpus=1       ← duplicated
```

Multiple NixOS config sources adding conflicting params. **Not harmless** —
`modeset=1` (last wins) defeats the VFIO FLR workaround from
`wyrm_gpu_lockup.md`. Source: `boot.kernelParams` adds `modeset=0`,
`hardware.nvidia.modesetting.enable = true` adds `modeset=1`. Fix: set
`hardware.nvidia.modesetting.enable = false` in
`nix/nixos/hosts/wyrm2/default.nix`. Still unfixed as of 2026-04-17.

### Host diagnostics

- Host PSI all zeros — no memory or I/O pressure
- Swap rate: ~5 pages/sec (negligible)
- CPU steal: 0%

## What Was Ruled Out

- **NVIDIA GPU issues** — no Xid errors, no DRM errors, VRAM nearly empty,
  GPUs responsive to nvidia-smi
- **Host memory pressure** — PSI 0%, swap negligible
- **ASPM** — disabled and confirmed in `LnkCtl`
- **PCIe link speed** — Gen 1 at idle is normal NVIDIA power management
- **Host kernel cmdline** — `iommu=pt` and `pcie_aspm=off` present and active

## Action Items

### Primary fix

1. **Switch `vga: qxl` to `vga: virtio`** in VM 110 config. This eliminates
   the QXL TTM bug entirely.

### Guest NixOS config cleanup

2. **Fix contradictory kernel params** — `nvidia-drm.modeset=0` and
   `nvidia-drm.modeset=1` are both present. Also `NVreg_OpenRmEnableUnsupportedGpus=1`
   is duplicated. Find the NixOS config sources and deduplicate.

### After restart

3. **Monitor `dmesg -w`** for TTM errors — should be gone with VirtIO-GPU
4. **Verify virtiofs `cache=never`** took effect

### Longer-term

5. **Reduce VM memory** to 48-64 GiB — 96 GiB is overkill (guest uses ~28 GiB)
6. **Verify PCIe ramps under load** — run a quick CUDA benchmark and check
   `nvidia-smi --query-gpu=pcie.link.gen.current --format=csv` to confirm
   Gen 5 x8 under load

## Hardware

- **GPUs**: 2x NVIDIA RTX 5090 (Blackwell, GB202, `10de:2b85`)
  - GPU 0: PCI `01:00.0` (IOMMU group 14) — ZOTAC (VBIOS `98.02.2E.80.4A`)
  - GPU 1: PCI `03:00.0` (IOMMU group 16) — Gigabyte (VBIOS `98.02.2E.00.D4`)
  - 32 GB VRAM each, host driver: `vfio-pci`
- **Guest driver**: NVIDIA 580.119.02 Open Kernel Module (only option for
  Blackwell — proprietary module does not support RTX 5090)
- **Guest kernel**: Linux 6.12.74 NixOS (SMP PREEMPT_DYNAMIC)

## VM Configuration (VM 110)

```
memory: 98304 (96 GiB)
balloon: 0
cores: 32
hostpci0: 0000:01:00.0,pcie=1  (RTX 5090)
hostpci1: 0000:03:00.0,pcie=1  (RTX 5090)
vga: virtio                         ← changed from qxl (2026-03-19)
virtiofs0: tankshare,cache=never    ← changed from cache=auto
virtiofs1: code,cache=never         ← changed from cache=auto
```

## Host Memory Accounting (for reference)

```
Total RAM:           123 GiB
wyrm2 memfd (Shmem):  96 GiB  (38 GiB RSS, 96 GiB VmLck)
talos-pve-cp-0 RSS:    3 GiB
ZFS ARC:               5 GiB  (max: 12.3 GiB)
Kernel slab:           2 GiB
Host userspace:        7 GiB  (firefox, gnome, spiceproxy, etc.)
Page tables:           0.7 GiB
Free:                 11 GiB
Swap used:            8.8 GiB (firefox 5.2G, talos 1.9G, pve 1.2G)
```

Not the cause of freezes, but worth optimizing eventually.

## Related

- <debug/atlas/wyrm_gpu_lockup.md> — prior RTX 5090 lockup investigation
- <debug/wyrm-oom/LOG.md> — prior wyrm (VM 100) OOM investigation
