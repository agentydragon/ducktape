# Wyrm2 remote desktop

## Current configuration

Remote desktop is parked because it is not being used. XRDP/Xfce and Sunshine
are disabled in Wyrm2's NixOS configuration; Rugged's dedicated FreeRDP
launcher and Moonlight package are also parked. These changes take effect on
those hosts' next NixOS switches. Sunshine previously captured an already
logged-in session; it did not provide pre-login access.

GNOME Remote Desktop (GRD) is also disabled: its system listener never worked
on Wyrm2 and was inactive in the November 2026 check. See
[listener forensics](grd_listener_forensics.md) for the observed readiness gate
and unresolved cause. SSH and the local physical GNOME desktop are separate
from these remote-desktop paths.

## Restore a remote session

- **XRDP/Xfce:** Uncomment both `rdpSession` and the XRDP/Xfce settings in
  `nix/nixos/hosts/wyrm2/default.nix`. Uncomment FreeRDP and the Wyrm2 launcher
  in `nix/home/hosts/rugged.nix` if needed. XRDP starts a separate X11 session
  with PAM authentication, including before a local login. The wrapper sets
  `HOME`, which XRDP's PAM environment did not set and Xfce needed to start.
- **Sunshine/Moonlight:** Uncomment `services.sunshine` in Wyrm2's host config
  and install `moonlight-qt` on the client. Sunshine needs a logged-in session.
  Its previous uinput rule granted the local user access to virtual input:
  `KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"`.
  Restore it if input injection is needed. For virtio-gpu KMS capture,
  `MUTTER_DEBUG_DISABLE_HW_CURSORS=1` previously made the cursor visible in the
  stream; restore only if that capture path needs it. Recheck CUDA/NVENC support,
  pairing, and firewall exposure after enabling.
- **GRD:** Restore the commented `grdConf`, service, SOPS secrets and tmpfiles
  rules in `nix/nixos/hosts/wyrm2/default.nix`. The encrypted TLS files remain
  in the repository. The prior system RDP listener never became ready: reproduce
  the readiness investigation before relying on it; `grd.conf` and TLS alone
  did not establish a working listener.

Review the access model before enabling a listener. The prior XRDP listener was
reachable on the trusted Nebula interface and required a system password; RDP
was not explicitly opened on other interfaces. The prior Sunshine module opened
its firewall ports. Verify the current rules and interface trust rather than
assuming those historical conditions still hold.

## Earlier observations

The [July 2026 investigation](history_2026_07.md) retains the detailed
connection, authentication, and troubleshooting notes.

- XRDP/Xfce worked over Nebula on 2026-07-19 after the `HOME` wrapper was added.
  `gnome-connections` crashed on XRDP drive redirection; FreeRDP worked.
- Sunshine and Moonlight previously streamed an existing Wyrm2 desktop session.
  This did not establish pre-login access or prove every encoder configuration.
- GRD's `grdctl --system rdp enable` failed while trying to persist a systemd
  unit under NixOS's read-only `/etc`. Further source and runtime investigation
  showed that listener binding also has a separate readiness gate; the missing
  readiness component was not identified. The EROFS error alone is not a proven
  explanation for the listener failing to bind.

The [direct-display bring-up](../direct_display_bringup/README.md) covers the
physical seat and GPU isolation. [Retirement notes](../../../docs/workstation_retired_features.md)
cover the other removed workstation features.
