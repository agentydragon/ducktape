# gnome-remote-desktop listener — why it never binds on wyrm2 (NixOS)

Live forensic investigation (2026-07-19), `root@wyrm2`. This file has been
**rewritten** after several wrong claims below were refuted by direct checks —
see **"Errors made during this investigation"**. Treat only the *Verified* lines
as fact; anything under *Open* is not yet confirmed.

## Verified facts (directly checked on wyrm2 / source)

- The RDP listener binds via `grd_daemon_maybe_enable_services →
  maybe_start_rdp_server → start_rdp_server → grd_rdp_server_start` (binds the
  port). `src/grd-daemon.c`. `maybe_start_rdp_server` returns early unless
  `is_daemon_ready()` is true, then unless `rdp-enabled`.
- `grd_daemon_system_is_ready` (`grd-daemon-system.c:125`) returns FALSE unless
  `remote_display_factory_proxy` + `display_objects` + `handover_manager_server` +
  `dispatcher_skeleton` are all set and their bus names are owned.
- **GDM *does* expose `RemoteDisplayFactory`.** Introspecting
  `org.gnome.DisplayManager` shows child nodes `LocalDisplayFactory`, `Displays`,
  and **`RemoteDisplayFactory`**; `/org/gnome/DisplayManager/RemoteDisplayFactory`
  is introspectable. (GDM creates it unconditionally — `gdm-manager.c:2438`,
  no meson gate.)
- The greeter runs as a **separate `gdm-greeter` user (UID 60578)**, not `gdm`
  (UID 132). Its user manager `user@60578.service` is reachable via
  `systemctl --user --machine=gdm-greeter@.host`.
- The handover daemon (`gnome-remote-desktop-handover.service`, a systemd **user**
  unit, `WantedBy=gnome-session.target`) **starts fine as `gdm-greeter`** when
  invoked via `--machine=gdm-greeter@.host` (confirmed: process running).
- Even with the handover running + the factory exposed, `grdctl --system status`
  still reports RDP **`Status: disabled`** → the system daemon's
  `is_daemon_ready` is still false.
- The `Enabled` D-Bus property is **read-only** (`Property "Enabled" is not
  writable`) — cannot be set directly.
- `grdctl --system rdp enable` fails `EROFS` on
  `/etc/systemd/system/graphical.target.wants/…` (read-only on NixOS) — but the
  source shows this is the **boot-persistence** step (the wants-symlink), separate
  from the bind path.

## Open (NOT yet verified — don't treat as fact)

- **Which `is_daemon_ready` component is actually missing** when the handover is
  running + factory exposed. The system daemon's journal at default log level only
  shows "Started GNOME Remote Desktop"; the "Daemon not ready" message is `g_debug`.
  Needs the system daemon run with `G_MESSAGES_DEBUG=all` (or its debug flag) to see
  which of `handover_manager_server` / `dispatcher_skeleton` / proxy setup is
  failing. **This is the real next step — not yet done.**
- Whether enabling the handover persistently for `gdm-greeter` + a GDM restart would
  change anything (not tested — would disrupt any seat0 session).

## Why xrdp works where GRD doesn't (still true)

xrdp spawns its own X server and runs Xfce in it — no `is_daemon_ready` gate, no
GDM handover, no read-only-`/etc` enable dance. That is why it's the working
remote-desktop path on wyrm2.

## Errors made during this investigation (corrected above)

I asserted several checkable claims before verifying them. Each was wrong:

1. **"`EROFS` blocks the listener"** — wrong. Source shows the bind is gated by
   `is_daemon_ready`; the `EROFS` is `grdctl`'s boot-persistence step, separate
   from binding.
2. **"Set `Enabled=true` over D-Bus to bypass `grdctl`"** — wrong. `Enabled` is
   read-only (`Property not writable`).
3. **"The gdm user's systemd user manager is not reachable"** — wrong. I targeted
   `gdm` (UID 132, no session); the greeter runs as `gdm-greeter` (UID 60578),
   whose user manager IS reachable. Wrong user, not a NixOS transport bug.
4. **"GDM doesn't expose `RemoteDisplayFactory`"** — wrong. I checked `busctl list`
   (which lists bus *names*, not objects); introspecting the object shows GDM does
   expose it.

Lesson: verify each link (read the source for the exact gate; introspect the actual
object; check the actual user) before asserting. The pattern of error was assuming
the next layer's behavior from the previous layer's symptom.
