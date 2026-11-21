# Atlas GPU Strategy

This is a historical investigation and plan, last updated during the July 2026 bring-up.
The direct display was subsequently wired and tested; the later single-seat design and
recovery notes are in <../../nix/debug/wyrm2/direct_display_bringup/README.md>, and the
recorded cable layout is in <../../desk/README.md>. The plans and pending steps below
describe the earlier state.

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
  see <../../nix/debug/wyrm2/spice/lag/README.md>). For Steam titles, **Steam Remote Play** can
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
      2026-07-02 — see <../../nix/debug/wyrm2/spice/autoresize.md>)
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
  and kills SPICE auto-resize (see <../../nix/debug/wyrm2/spice/autoresize.md>). Options: accept
  the pin, or configure the NVIDIA output without persisting (TBD at bench
  time).
- **Optional refinement**: run games in gamescope directly on the NVIDIA
  DRM device (own little kiosk seat) instead of extending GNOME onto the
  monitor — desktop and game display fully independent. Decide after the
  basic extended-desktop variant works.

Remaining work: plug 2 cables, OSD dual-KVM binding, `modeset` revert +
rebuild + VM restart, `qm set` USB port passthrough, bench test.

**Subsequent bring-up and design** — notes: <../../nix/debug/wyrm2/direct_display_bringup/README.md>.

## Earlier options (April 2026, not taken)

When VFIO crashes made two-GPU passthrough look unsafe, the options were host-native GPUs with an
LXC bind-mount, VFIO to one small VM, debugging VFIO first, and bare-metal NixOS. None was needed:
the host mitigations recorded in <black_screen_lockup.md> made passthrough to wyrm2 stable (zero
host-level crashes in the 28 days to April). The open next steps there (chipset heatsink, an HBA
for SATA) and the guest-side GPU lockups in <wyrm_gpu_lockup.md> are the remaining work.

## Related Debug Files

- <black_screen_lockup.md> — chipset PCIe fabric instability (incidents 1–10)
- <wyrm_gpu_lockup.md> — GPU lockwatch timeouts under VFIO
- <../../docs/atlas_network.md> — physical-link diagnostics (separate issue)
