# Wyrm2 display and remote access

No graphical remote-access path is currently confirmed usable: the operator reports
no working remote desktop, and SPICE has not been validated.

The declarative configuration is in
[`default.nix`](../nixos/hosts/wyrm2/default.nix), the VM's display and USB
attachments in [`proxmox-vms.tf`](../../cluster/terraform/main/proxmox-vms.tf),
and physical wiring in [`desk/README.md`](../../desk/README.md).

## Local display

The display RTX 5090 (`01:00.0` in the guest) drives the FV43U on `seat0`.
GNOME/Mutter ignores the virtio display and spare 5090 via `mutter-device-ignore`
udev tags. Sway ignores that tag and would need explicit `WLR_DRM_DEVICES`
selection before using the same isolation.

Keep the monitor on the GPU games render on: the July 2026 bring-up could not
select between the identical 5090s using gamescope's `vendor:device` selector.
The FV43U's USB uplink binding can revert to USB-C; check its OSD when the
USB-B keyboard/camera path disappears. Keep Steam's **GPU accelerated rendering
in web views** enabled; the observed CPU path rendered Big Picture at about 5 FPS.
Keep game prefixes on the SSD; small-file I/O on the HDD virtiofs share was slow.

## Remote desktop remains unresolved

As checked on Wyrm2 on 2026-10-28, `xrdp` and `xrdp-sesman` are active and
TCP 3389 is listening. This proves a listener, not successful client login or a
usable desktop. GNOME Remote Desktop is inactive. The July investigation
recorded a working xrdp session; current end-to-end acceptance remains open.

The configured xrdp path starts a separate X11 Xfce session and uses the system
password through PAM. Its `rdpSession` wrapper supplies `HOME`, which was missing
from the xrdp PAM environment during the original black-screen investigation.
It does not share the local Wayland/GNOME desktop or need auto-login.

For a future client acceptance test, Rugged has a **wyrm2 (RDP)** desktop entry.
Its command is shown here as a candidate test, not a verified access path:

```bash
xfreerdp /v:10.42.0.20 /u:agentydragon /cert:tofu /dynamic-resolution
```

Although `services.xrdp.openFirewall` is not enabled, the worker module trusts
`nebula1`, `cilium_host`, and `lxc+`; an SSH tunnel is not required from a mesh
peer. Do not interpret the closed public port as restricting access to localhost.
`/cert:tofu` uses trust on first use for the self-signed certificate. The July
record reports a `gnome-connections`/gtk-frdp crash; use FreeRDP for acceptance.

Sunshine captures an existing local session; it cannot provide pre-login access.
GNOME Remote Desktop is a separate, unvalidated path. Its declarative TLS/config
wiring replaced the old setup oneshot. The
[remote-desktop investigation](../debug/wyrm2/remote_desktop_wyrm2.md) and
[GRD readiness investigation](../debug/wyrm2/grd_listener_forensics/README.md)
retain evidence and open questions. In particular, `grdctl`'s read-only-`/etc`
error concerns boot persistence; it does not establish why the listener failed.

## Recovery and diagnostics

- [Login returns to the greeter](wyrm2_login_recovery.md): inspect stale logind
  sessions and the user manager's `graphical-session.target`.
- Capture seat/DRM/logind state with
  `sudo bash nix/debug/wyrm2/direct_display_bringup/seat-diag.sh` from the repo root.
- After changing seat tags, a rebuild alone may leave stale `/run/udev/data`
  entries and logind state. Reboot to re-enumerate; before attempting a live
  migration, identify the affected DRM devices rather than reusing old `cardN`
  numbers from a previous boot.

## Historical SPICE resize diagnostics

These July findings do not establish that SPICE works on the current setup.
For a GNOME session using the virtual display, a saved `~/.config/monitors.xml`
configuration can override the client's preferred mode and stop window-following
resize without an error. Back up that file and its `~` copy, remove the virtual
monitor configuration, then start a new GNOME session: Mutter retains it in
memory until logout. Setting a fixed resolution in Settings intentionally opts
out of window-following resize. This was diagnosed on GNOME 49 in July 2026;
it does not imply that the current physical `seat0` uses SPICE.

## Constraints from the multiseat investigation

The July 2026 experiment found that non-`seat0` seats have no VTs; VT-based
session launchers failed there. GDM's greeter could start on that seat but its
user-session handoff failed. SDDM 0.21.0 also supplied an inappropriate virtual
console `PAM_TTY`, and the tested Plasma Login Manager used fixed-name user
units that could not start a second greeter. These findings explain the single
local seat; recheck upstream versions before retrying multiseat. The original
bring-up narrative and per-manager evidence are in Git history.
