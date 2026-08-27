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

## Current State (Jul 2026)

Post-move recheck (2026-07-02, new apartment) + gaming plan:

- **Gaming plan (rev 2, 2026-07-02 evening): direct display output.** Plug a
  5090 into the FV43U's free DP 1.4 input; keyboard reaches wyrm2 via the
  monitor's second (USB-B) hub uplink into an atlas USB port passed through
  to VM 110. Streaming demoted to desktop-convenience transport. See "Plan:
  direct display output" below. Rationale: goal is _playing games on this
  machine_, not streaming — and the streaming path turned out to have no
  hardware encoder (below).
- ~~Gaming plan rev 1: Sunshine (wyrm2) + Moonlight (atlas) streaming~~ —
  built and works, but **only with software x264**: Sunshine's NVENC needs
  KMS capture and CUDA on the _same_ DRM device, and the display lives on
  virtio-gpu while CUDA lives on the 5090s (`"card1" is not a CUDA device`).
  VA-API via virgl also dead: Proxmox's virglrenderer isn't built with video
  encode passthrough → no usable encode profiles in the guest. Kept as the
  casual/desktop path. Looking Glass remains N/A (no Linux guest support,
  see `spice_lag/README.md`). For Steam titles, **Steam Remote Play** can
  NVENC on the 5090 regardless of display placement (captures in-pipeline).
- **`nvidia-drm.modeset` conflict fixed** in `nix/nixos/hosts/wyrm2/default.nix`
  (`hardware.nvidia.modesetting.enable = false`; it was appending `modeset=1`
  after the explicit `modeset=0`, last-wins). Switch applied; takes effect on
  next wyrm2 boot.
- **VM 110 display → `vga: virtio-gl`** (VirGL: guest GL executes on atlas's
  AMD iGPU) so Mutter composites on the iGPU regardless of NVIDIA GPU state —
  with `modeset=0`, gnome-shell may lose the NVIDIA render path, and plain
  `virtio` would fall back to llvmpipe (the `spice_audio` choppiness mode).
  Same stop/start activates this + `modeset=0` + Sunshine.
- **Sunshine enabled** in wyrm2 NixOS config (`services.sunshine`).
- Atlas-side display currently runs 4K@60 (see `desk/debug/build_log.md`
  2026-07-02 entry); fine for the 60 fps target.

### Post-reboot checklist (wyrm2) — run 2026-07-02

- [x] `cat /proc/cmdline` — exactly one `nvidia-drm.modeset=0` ✓
- [x] `nvidia-smi` — both GPUs healthy, fully idle (nothing composites on
      them anymore)
- [x] Compositor renders via virgl (gnome-shell GBM on virtio card; GL
      driver reports "Mesa virgl (AMD Radeon Graphics radeonsi)"), no
      llvmpipe
- [x] Sunshine up, Moonlight paired from atlas (x264 software encode only —
      see above)
- [x] SPICE auto-resize follows window again (`monitors.xml` deleted
      2026-07-02 — see <spice_autoresize.md>)
- [ ] Watch for guest GPU lockups under sustained load (`modeset=0` was the
      suspected contributor — note the direct-display plan below reverts to
      `modeset=1`, ending this experiment)

### Sunshine deployment notes (2026-07-02)

- `services.sunshine` needs `package = pkgs.sunshine.override
{ cudaSupport = true; }` for NVENC to even be probed (moot here, see
  above, but kept for a future NVIDIA-display setup).
- **nixpkgs gap**: the sunshine package ships no udev rules, so the module's
  `services.udev.packages = [ package ]` is a no-op and Sunshine gets
  "Permission denied" creating virtual keyboard/mouse (= input silently
  dead). Fixed with upstream's rule via `services.udev.extraRules`
  (uinput + `uaccess` tag). After first deploy the existing `/dev/uinput`
  needs `udevadm trigger --action=change --sysname-match=uinput` (or a
  reboot) for the ACL to appear.
- `min_log_level = 1` currently in `~/.config/sunshine/sunshine.conf` for
  debugging — remove when done.

## Plan: direct display output (2026-07-02, agreed)

Goal is playing games on this machine; streaming was a workaround. Instead:
5090 drives the FV43U directly, desktop stays on SPICE/virtio untouched.

- **Video**: 5090 DP-OUT → FV43U DP 1.4 input (spare Ivanky 8K DP cable on
  hand). Native 4K144 + VRR available; no encode/decode anywhere.
- **Input**: TEX Shura stays on the monitor hub. The hub's second uplink
  (USB-B, currently unused) → atlas rear USB port → Proxmox **port-pinned**
  passthrough to VM 110 (`qm set 110 --usbN host=<bus>-<port>` so anything
  on that port lands in wyrm2). The FV43U "dual KVM" binds USB uplinks to
  video inputs in the OSD — ideally one button switches video + hub
  together between work (USB-C → TB4 KVM) and game (DP + USB-B → wyrm2).
  Bench-verify the OSD binding behavior; camera (C920) rides the hub and
  follows the keyboard to wyrm2 in game mode.
- **`nvidia-drm.modeset=1` revert required** — a display on the NVIDIA card
  needs KMS. Evidence this is acceptable: the 28-day host-stable streak ran
  with `modeset=1` active (the conflict meant `=1` won all along); what we
  lose is the guest-lockup `modeset=0` experiment. Guest lockups are also
  much softer now: with virtio-gl the desktop survives (no llvmpipe hell),
  a lockup "just" kills CUDA/games until VM reboot.
- **HDMI/DP carry no USB** (only USB-C DP-Alt does) — that's why the USB-B
  copper run is needed. The 5090 has no USB port (VirtualLink died with
  RTX 20).
- **SPICE must keep working** (hard requirement): virtio stays the primary
  desktop display, unaffected. **Known tension**: arranging the new second
  monitor in GNOME Settings writes `monitors.xml`, which re-pins Virtual-1
  and kills SPICE auto-resize (see <spice_autoresize.md>). Options: accept
  the pin, or configure the NVIDIA output without persisting (TBD at bench
  time).
- **Optional refinement**: run games in gamescope directly on the NVIDIA
  DRM device (own little kiosk seat) instead of extending GNOME onto the
  monitor — desktop and game display fully independent. Decide after the
  basic extended-desktop variant works.

Remaining work: plug 2 cables, OSD dual-KVM binding, `modeset` revert +
rebuild + VM restart, `qm set` USB port passthrough, bench test.

**Bring-up in progress** — running notes: <direct_display_bringup/README.md>.

## Prior State (Apr 2026)

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
- ~~**`nvidia-drm.modeset` conflict**~~: fixed 2026-07-02
  (`modesetting.enable = false`), pending reboot — see Current State above

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
- <wyrm_gpu_lockup.md> — GPU lockwatch timeouts under VFIO
- <ethernet_recurring/README.md> — network drops (cable issue, separate)
