# gnome-remote-desktop listener — why it never binds on wyrm2 (NixOS)

Forensic investigation (2026-07-19), **source- + empirically-confirmed**. Corrects
the earlier "EROFS blocks the listener" claim in `../remote-desktop-wyrm2.md`
Path A, which was **wrong** — the EROFS is a red herring for the _bind_.

## TL;DR

The RDP listener binds only when `grd_daemon_system_is_ready()` is true, which
requires GDM's `RemoteDisplayFactory` — exposed by a GRD handover daemon that must
autostart **inside the GDM greeter session**. On NixOS that greeter-session
handover never starts (nixpkgs [#504490](https://github.com/NixOS/nixpkgs/issues/504490)),
so the factory is never on the bus, `is_daemon_ready` is always false, and the RDP
server never starts. The `grdctl --system rdp enable` `EROFS` is a **separate,
secondary** issue (boot-persistence of the wants-symlink), **not** the bind
blocker. xrdp (its own X session, no handover) is the working path.

## Black-box findings (wyrm2, root, 2026-07-19)

- The daemon is **inactive at boot**: the unit is `linked` but not `enabled` (the
  `graphical.target.wants` symlink is missing), and #3424 removed the oneshot that
  used to start it. (Earlier "active" observations were from a prior boot where the
  oneshot had started it.)
- Started manually, the daemon runs with `Enabled = false`, nothing on 3389.
- The `Enabled` D-Bus property is **read-only** (`Property "Enabled" is not
writable`) — cannot `busctl set-property` it to bypass `grdctl`.
- `grdctl --system rdp enable` → `EROFS` on the wants-symlink; `Enabled` stays
  false.
- **Confound:** in the live test **xrdp held 3389**, so a GRD bind would have been
  `EADDRINUSE` — a real confound (the "port grabbed" suspicion was valid there).
- GDM login itself works: `org.gnome.DisplayManager` is owned, the gdm-greeter
  session is running. But there is **no `RemoteDisplayFactory` on the bus** and
  **no GRD handover process in the greeter session** (the greeter autostart has no
  handover/remote-desktop entry).
- Side-finding: **sunshine runs in the gdm-greeter session** (`.sunshine-wrapp`) —
  relevant to the separate "does Sunshine need a logged-in seat?" question.

## Source (gnome-remote-desktop 50.1) — the actual mechanism

The listener is **not** bound by `grdctl enable`. It is bound by:
`grd_daemon_maybe_enable_services` → `maybe_start_rdp_server` → `start_rdp_server`
→ `grd_rdp_server_start` (binds the port). `src/grd-daemon.c`.

`maybe_start_rdp_server` (`grd-daemon.c:435`) gates on two things, in order:

1. `is_daemon_ready()` — else logs `"Daemon not ready, not starting RDP server"`
   and returns (no bind).
2. `rdp-enabled` (from settings/`grd.conf`).

For the **system** daemon, `is_daemon_ready` = `grd_daemon_system_is_ready`
(`grd-daemon-system.c:125`), which returns **FALSE** unless **all** of
`remote_display_factory_proxy` (`GrdDBusGdmRemoteDisplayFactory`) +
`display_objects` + `handover_manager_server` + `dispatcher_skeleton` are present
and their bus names are **owned** (lines 139–155). Those come from GDM's
`RemoteDisplayFactory` — GDM's "create a new session for an incoming remote
connection" interface — which is exposed by GRD's handover daemon running **inside
the GDM greeter session**. That greeter-session handover never starts on NixOS
(#504490), so the factory is never on the bus → `is_daemon_ready` always false →
no RDP server → no listener.

`grdctl --system rdp enable` does **two separate** things:

- sets `rdp-enabled` in settings (would trigger `maybe_enable_services` → bind,
  **if the daemon were ready**), and
- a systemd-enable (the wants-symlink → `EROFS`) that is purely
  **boot-persistence**.

So the `EROFS` does **not** prevent binding — it prevents boot-persistence. The
bind is gated by `is_daemon_ready` (the GDM handover), which is the real NixOS
blocker.

## Correction to the prior diagnosis

- "EROFS blocks the listener": **wrong**. The bind is gated by `is_daemon_ready`
  (GDM handover, #504490); the EROFS is secondary (boot-persistence only).
- The `Enabled` property is read-only — the "set Enabled=true over D-Bus to bypass
  grdctl" idea does not work.
- The daemon doesn't even run at boot on current config (unit not enabled + oneshot
  removed), so it never gets the chance.
- This is distinct from GDM login working — the handover needs the greeter-session
  GRD component, which is a separate autostart that NixOS doesn't wire.

## Why xrdp works where GRD doesn't

xrdp spawns its **own** X server and runs Xfce in it — no GDM handover, no
`is_daemon_ready` gate, no read-only-`/etc` enable dance. That is why it is the
working remote-desktop path on wyrm2; GRD Remote Login is blocked upstream on NixOS
until the GDM greeter-session handover (#504490) is wired.
