# Wyrm GPU Lockup Investigation

## Problem

The wyrm VM (VM 100 on atlas Proxmox host) has 2x NVIDIA RTX 5090 GPUs passed
through via VFIO. The GPUs intermittently lock up, producing kernel log messages:

```
RC watchdog: GPU is probably locked!  Notify Timeout Seconds: 7
Assertion failed: (status == NV_OK) || (status == NV_ERR_GPU_IN_FULLCHIP_RESET) @ rs_client.c:844
Assertion failed: (status == NV_OK) || (status == NV_ERR_GPU_IN_FULLCHIP_RESET) @ rs_server.c:259
```

When locked, `nvidia-smi` fails, CUDA reports 0 devices, and the GPUs are
completely unresponsive until reboot.

## Hardware

- **Host**: atlas (Proxmox VE 8, AMD CPU, systemd-boot)
- **GPUs**: 2x NVIDIA RTX 5090 (Blackwell, GB202, `10de:2b85`)
  - GPU 0: PCI `01:00` (IOMMU group 14)
  - GPU 1: PCI `03:00` (IOMMU group 16)
- **Driver**: 580.82.09 (open kernel module)
- **Passthrough**: VFIO, `hostpci0=01:00,pcie=1` + `hostpci1=03:00,pcie=1`
- **VM**: wyrm (Pop!\_OS, kernel 6.16.3, q35 machine, OVMF BIOS)
- **Host BIOS**: Above 4G Decoding enabled, Resizable BAR enabled

## Issues Found

### 1. Kernel cmdline never applied (critical)

Atlas uses **systemd-boot**, not GRUB. The previous Ansible config wrote to
`/etc/default/grub` which has no effect. The actual kernel cmdline is in
`/etc/kernel/cmdline` and is applied via `proxmox-boot-tool refresh`.

As of investigation, the running kernel cmdline was:

```
root=ZFS=rpool/ROOT/pve-1 boot=zfs
```

Missing: `amd_iommu=on`, `iommu=pt`, and the new `pcie_aspm=off`. This means
IOMMU passthrough mode was never actually enabled — the GPUs were running
without proper IOMMU isolation, which could itself cause instability.

### 2. IOMMU groups are clean

Each GPU and its audio function are in their own IOMMU group — no ACS override
needed:

- Group 14: `01:00.0` (VGA) + `01:00.1` (Audio)
- Group 16: `03:00.0` (VGA) + `03:00.1` (Audio)

## Root Cause Analysis

Most likely cause: **missing `iommu=pt` and `pcie_aspm=off`**. Without IOMMU
passthrough mode, DMA translations go through the full IOMMU path for all
devices (not just VMs), adding latency and potential for faults. Combined with
PCIe ASPM allowing the GPU link to enter low-power states that VFIO can't
recover from.

Contributing factors:

- **Blackwell is new** — driver 580.x VFIO support may have bugs
- **Open kernel module** — `nvidia-open` is less mature for passthrough than
  the proprietary module
- **2 GPUs** — doubles the surface area for PCIe link issues

## Applied Fix

Updated `ansible/atlas.yaml` to write `/etc/kernel/cmdline` (systemd-boot)
instead of `/etc/default/grub`:

```
root=ZFS=rpool/ROOT/pve-1 boot=zfs amd_iommu=on iommu=pt pcie_aspm=off
```

After changing, run the playbook and reboot the host:

```bash
cd ansible
ansible-playbook atlas.yaml --tags gpu_passthrough,iommu
# Then reboot atlas
```

Or apply manually on atlas:

```bash
echo "root=ZFS=rpool/ROOT/pve-1 boot=zfs amd_iommu=on iommu=pt pcie_aspm=off" > /etc/kernel/cmdline
proxmox-boot-tool refresh
reboot
```

## If ASPM Fix Doesn't Help

Try these in order:

### 1. Disable PCIe AER (Advanced Error Reporting)

Error storms from PCIe can cascade and lock the GPU. Add to kernel cmdline:

```
pci=noaer
```

### 2. Pin GPUs to `vfio-pci` at Boot

Currently relying on nvidia driver blacklist. Explicit VFIO binding is more
reliable. Add to `/etc/modprobe.d/vfio.conf`:

```
options vfio-pci ids=10de:2b85,10de:22e8
```

### 3. Try the Proprietary NVIDIA Kernel Module

The open kernel module (`nvidia-open`) is less battle-tested for VFIO
passthrough. In the VM, switch to the proprietary module:

```bash
sudo apt install nvidia-driver-580  # proprietary, not -open
```

### 4. Disable Resizable BAR in Host BIOS

RTX 5090 has very large BARs. While ReBAR improves GPU performance, it can
cause issues with VFIO memory mapping. Try disabling it in the host BIOS to
see if stability improves (keep Above 4G Decoding enabled).

### 5. Kernel Parameter: `video=efifb:off`

Prevents the host from touching the GPU framebuffer at all during boot:

```
video=efifb:off
```

## Diagnostics

After reboot, verify the fix:

```bash
# On atlas host — verify cmdline applied
cat /proc/cmdline
# Should show: ... amd_iommu=on iommu=pt pcie_aspm=off

# On atlas host — verify IOMMU enabled
dmesg | grep -i -E 'AMD-Vi|IOMMU'

# On wyrm VM
nvidia-smi                                    # Should show both GPUs
nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.width.current --format=csv

# On atlas host — check for errors
dmesg | grep -i -E 'AER|vfio|iommu|error.*gpu|aspm'
journalctl -k | grep -i nvidia
```

Monitor `dmesg -w` on both host and guest during GPU load to catch early
warnings.

## Timeline

- **2026-02-01**: First investigated. Found GPUs locked (`initial_count=0` in
  Ollama, `nvidia-smi` failing). Initially misdiagnosed as Ollama/CUDA library
  issue; actual cause was hardware-level GPU lockup visible in kernel logs.
  Discovered atlas uses systemd-boot, not GRUB — kernel cmdline params
  (`amd_iommu=on iommu=pt`) were never actually applied. Fixed Ansible to
  target `/etc/kernel/cmdline` + `proxmox-boot-tool refresh`, added
  `pcie_aspm=off`.
