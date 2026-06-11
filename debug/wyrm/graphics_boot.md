# Wyrm2: GDM fails to start + SPICE resize (2026-03-10)

## Problem: GDM crash on boot

GDM 49.2 crashes with `SIGTRAP` in `get_fallback_session_name()`:

```
GdmSession: no session desktop files installed, aborting...
```

Chain:

1. NixOS sees `videoDrivers = [ "nvidia" ]` → writes `WaylandEnable=false` in GDM config
2. GDM only looks for X11 sessions in `xsessions/`
3. **GNOME 49 removed X11 sessions** — `gnome-xorg.desktop` no longer ships
4. `xsessions/` empty, `wayland-sessions/` has `gnome.desktop` → zero usable sessions → crash

## Display hardware

**Update 2026-03-20**: Virtual display switched from QXL to VirtIO-GPU
(`vga: virtio,memory=256`) to fix QXL TTM freezes. See
<debug/atlas/wyrm2-freezes.md>. Proxmox noVNC console works with smooth
composited desktop rendering and dynamic resize. The 256MB VGA memory is
required — the 16MB default caused `INVALID_RESOURCE_ID` errors.

Previously QXL-driven:

| DRM card | Device          | Status                    |
| -------- | --------------- | ------------------------- |
| card0    | NVIDIA RTX 5090 | disconnected              |
| card1    | **QXL**         | **connected** (Virtual-1) |
| card2    | NVIDIA RTX 5090 | disconnected              |

NVIDIA GPUs are headless compute (VFIO passthrough, no monitors). The NixOS
auto-disable of Wayland for NVIDIA is wrong for this setup — the NVIDIA GPUs
aren't driving any display.

## Fix applied

```nix
# nix/nixos/hosts/wyrm2/default.nix
services.displayManager.gdm.wayland = true;
```

Overrides NixOS's NVIDIA auto-disable. GNOME 49 is Wayland-only, no alternative.

## SPICE resize: works on Wayland (with workaround)

Display resize works. The nixpkgs `spice-vdagent` is built X11-only (no GTK/Wayland
build flags), but it connects to XWayland and uses mutter's D-Bus interface
(`vdagent_mutter_get_resolutions`) for the actual resize.

### NixOS module gap

`services.spice-vdagentd.enable` only starts the system daemon. The per-user
`spice-vdagent` process relies on an XDG autostart `.desktop` file, but **GNOME 49
ignores it** (`X-GNOME-Autostart-Phase` is no longer honored).

Fix: added a `systemd.user.services.spice-vdagent` unit in `vm-hardware.nix` that
starts the user agent after `graphical-session.target`.

Upstream tracking:

- [nixpkgs #481078](https://github.com/NixOS/nixpkgs/issues/481078) — spice-vdagent fails on GNOME
- [nixpkgs PR #266080](https://github.com/NixOS/nixpkgs/pull/266080) — proposed `services.spice-vdagent.enable` (stale)

### Clipboard sharing

Broken on Wayland — upstream limitation. spice-vdagent can't access the Wayland
clipboard (no standard protocol; `wlr-data-control` is wlroots-only, not GNOME).
See [upstream issue #26](https://gitlab.freedesktop.org/spice/linux/vd_agent/-/issues/26).

### Resize flow

**Update 2026-03-20**: Resize works with virtio-gpu over noVNC after increasing
VGA memory to 256MB. Virtio-gpu handles resize natively via DRM mode changes —
no spice-vdagent needed. The flow below is the legacy QXL/SPICE path.

1. SPICE client tells QEMU desired resolution
2. QEMU updates QXL's available DRM modes
3. spice-vdagent (via XWayland + mutter D-Bus) notifies the desktop environment
4. Mutter handles it via QXL DRM hotplug

GNOME-specific — XFCE/KDE never implemented their side
([Red Hat bug 1290586](https://bugzilla.redhat.com/show_bug.cgi?id=1290586)).

## SSH access

```bash
ssh -J root@10.0.182.102 root@10.0.106.97
```

## Sources

- [GNOME X11 Session Removal FAQ](https://blogs.gnome.org/alatiera/2025/06/23/x11-session-removal-faq/)
- [Red Hat bug 1290586: QXL resize works on GNOME, not XFCE/KDE](https://bugzilla.redhat.com/show_bug.cgi?id=1290586)
- [spice-vdagent mutter D-Bus commit](https://cgit.freedesktop.org/spice/linux/vd_agent/commit/?id=73bf8367268e7ef5a00fd23674b0a8700d0e4a85)
