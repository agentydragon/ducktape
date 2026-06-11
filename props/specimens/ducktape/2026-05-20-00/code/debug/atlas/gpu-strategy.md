# Atlas GPU Strategy

## Goal

Use 2x RTX 5090 GPUs flexibly:

- **Default**: Available to k8s cluster (Ollama, ML workloads)
- **On demand**: Switch one or both to a Windows/gaming VM without rebooting atlas

## Hardware

- **Motherboard**: ASUS ProArt X870E-CREATOR WIFI (Rev 1.xx)
- **CPU**: AMD Ryzen 9 9950X3D (16-core)
- **RAM**: 128 GB (structurally tight — wyrm2 alone takes 112 GB)
- **GPUs**: 2x NVIDIA RTX 5090 (GB202, Blackwell)
  - GPU 0: `01:00.0` → IOMMU group 14 (VGA + audio, clean)
  - GPU 1: `03:00.0` → IOMMU group 16 (VGA + audio, clean)
- **BIOS**: AMI v1512 (2025-06-05)
- **Kernel cmdline**: `amd_iommu=on iommu=pt pcie_aspm=off`

## Current State (Apr 2026)

- **GPUs passed through to wyrm2 (VM 110)** via VFIO — both RTX 5090s
- **VFIO passthrough stable** — zero host-level crashes in 28 days (7 boots
  since Mar 20). Full production config: wyrm2 (96 GiB, 2x GPUs) + Talos CP VM
- **VM autostart re-enabled** (`onboot: 1` on VM 110)
- **ASPM L1 and PCIe runtime PM disabled** via udev rules
- **SATA DIPM disabled** via `ahci.mobile_lpm_policy=1` (kernel 6.17 default
  was `min_power` which caused chipset instability — see `black_screen_lockup.md`)
- **Guest-side GPU lockups still occur** — `nvidia-smi` shows ERR after
  extended uptime. Guest VM reboot recovers. When GPUs are locked, gnome-shell
  falls back to llvmpipe (software rendering), causing high CPU usage and audio
  choppiness. See `spice_audio/README.md`
- **`nvidia-drm.modeset` conflict**: NixOS config sets both `modeset=0`
  (boot.kernelParams) and `modeset=1` (hardware.nvidia.modesetting.enable=true).
  Last wins → `modeset=1` is active, defeating the VFIO FLR workaround. Needs
  fix in `nix/nixos/hosts/wyrm2/default.nix`

## Known Problems

### 1. ~~Chipset PCIe fabric instability (incidents 1–6)~~ — MITIGATED

Slow-onset: SATA errors start ~5-6h after boot, escalate to full chipset dropout
(SATA + USB + NIC all on same root port `0000:02.1`).

**Root cause**: Kernel 6.17 AHCI driver defaults to `min_power` LPM policy,
enabling SATA DIPM. DIPM link power state transitions destabilize the chipset.
**Fix**: `ahci.mobile_lpm_policy=1` (max_performance). Zero SATA errors in 28+
days since deployment. Also: ASPM L1 disabled, PCIe runtime PM disabled.

### 2. ~~VFIO GPU reset crashes (incidents 7–10, 12, 14)~~ — MITIGATED

System freezes within 30–120 seconds of VFIO GPU reset. Known Blackwell FLR bug.
**Mitigated by**: combination of `ahci.mobile_lpm_policy=1`, `pcie_aspm=off`,
PCIe bridge D3cold disabled, and `nvidia-drm.modeset=0` (though last is
currently overridden — see above). Zero VFIO crashes in 28+ days with full 2x
GPU passthrough.

### 3. Guest-side GPU lockups (ongoing)

Both RTX 5090s intermittently lock up inside the wyrm2 guest. `nvidia-smi` shows
ERR, dmesg shows `GPU_IN_FULLCHIP_RESET` assertions. Requires VM reboot to
recover. This is different from the host-level VFIO crash — the host remains
stable. See `wyrm_gpu_lockup.md`.

### 4. Blackwell VFIO is bleeding edge

RTX 5090 + open kernel module + VFIO is very new. Proprietary driver does not
support RTX 5090 at all — open module is the only option. Driver 580.142
currently in use.

## What We Don't Know

- [ ] Can NVIDIA drivers load on the Proxmox host? (`nvidia-smi` from host)
- [x] Does VFIO with only 1 GPU also crash? → Tested in incident 15; 1 GPU
      survived 33 min alone. With mitigations, 2 GPUs are now stable (28+ days)
- [x] Is there a BIOS update? → Updated to BIOS 2102 (AGESA 1.3.0.0a) on
      2026-03-11
- [x] Does the IOMMU passthrough fix change VFIO stability? → Yes, part of
      the fix set that stabilized the system
- [ ] Would proprietary NVIDIA driver help? → Not applicable; proprietary
      driver does not support RTX 5090 (Blackwell)
- [ ] Is it thermal? (chipset heatsink condition unknown)
- [ ] Would a PCIe HBA card for SATA reduce root-port contention?
- [ ] What causes guest-side GPU lockups? (different from host VFIO crashes)

## Options

### Option A: Host-native GPU + LXC bind-mount (no VFIO for k8s)

Load NVIDIA drivers on atlas host. Expose GPUs to k8s via the lxc-k8s-test
container with `/dev/nvidia*` bind-mounts. For gaming, stop the container,
unload host driver, VFIO-bind one GPU, start a Windows VM.

**Pros**:

- No VFIO for the common case — avoids the crash trigger entirely
- LXC GPU access is well-supported (bind-mount, no reset needed)
- Host `nvidia-smi` gives direct visibility

**Cons**:

- Gaming still needs VFIO (may still crash)
- Driver unload → VFIO rebind → VM start is multi-step
- Host NVIDIA driver + Proxmox may have quirks
- No GUI from LXC (headless only, but k8s workloads are headless anyway)

### Option B: VFIO to a single lightweight VM

Pass only 1 GPU via VFIO to a small NixOS VM (k8s worker). Other GPU idle
or host-native.

**Pros**: Reduces VFIO surface (1 GPU may not trigger the crash).
**Cons**: Still uses VFIO. Only 1 GPU for k8s. Untested whether 1 GPU is stable.

### Option C: Debug VFIO first, then decide

1. Update BIOS
2. Reboot with the IOMMU passthrough fix (already applied, not yet rebooted?)
3. Test VFIO with 1 GPU only
4. Try proprietary NVIDIA driver
5. Check chipset thermals
6. If all fails, RMA motherboard

**Pros**: May unlock the original plan.
**Cons**: Could be a long rabbit hole.

### Option D: Bare-metal Linux, no Proxmox

Ditch Proxmox. Run NixOS directly on hardware. k8s worker + desktop on same
machine. Windows gaming via dual-boot or GPU-passthrough with libvirt/QEMU.

**Pros**: No hypervisor overhead. Direct GPU access. Simplest for daily use.
**Cons**: Loses Proxmox VM management. Can't run Windows VM alongside Linux
without VFIO (which may still crash). Dual-boot means rebooting for Windows.

## Recommended Path

**Start with Option A** (host-native GPU, lowest risk), but first:

1. **Reboot atlas** to pick up the IOMMU passthrough fix
2. **Install NVIDIA drivers on the host** and test `nvidia-smi`
3. **Set up LXC GPU bind-mount** (per `TODO.md` plan)
4. **Verify k8s can use GPUs via LXC**

If host-native works, we have a stable k8s GPU path. Gaming can be tackled
separately (Option C debugging) without blocking the primary use case.

## Related Debug Files

- <black_screen_lockup.md> — chipset PCIe fabric instability (incidents 1–10)
- <locked-gpus/NOTES.md> — VFIO IOMMU misconfiguration discovery + fix
- <wyrm_gpu_lockup.md> — GPU lockwatch timeouts under VFIO
- <ethernet_recurring/README.md> — network drops (cable issue, separate)
