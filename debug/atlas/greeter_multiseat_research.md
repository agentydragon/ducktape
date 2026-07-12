# Greeter multi-seat capabilities — research report

**Question:** which display managers / greeters can run **two simultaneous graphical
sessions for the same user on the same machine**, one per logind seat (specifically:
seat0 = SPICE virtual display, seat-game = physical RTX 5090 + keyboard on wyrm2)?

**Date:** 2026-07-11. **Method:** source reading. Cloned into `/code/github.com/`:
GDM 49.2 (`GNOME/gdm`), gnome-shell 49.4 (`GNOME/gnome-shell`), greetd
(`kennylevinsen/greetd`), LightDM (`canonical/lightdm`), SDDM (`sddm/sddm`), systemd
v258 (`systemd/systemd`). Line numbers below are from those checkouts.

## The two independent axes

The problem decomposes into two things a DM must satisfy, and they are **independent**:

1. **VT-less multi-seat**: can it spawn a greeter/session on a _non-seat0_ logind seat?
   Non-seat0 seats have no VTs (`CAN_TTY=0`) — see <direct_display_bringup.md> for the
   VT/seat relationship. A DM whose session model is "allocate a VT, `chvt` to it"
   cannot drive such a seat. This requires following logind's `ListSeats` / `SeatNew` /
   `SeatRemoved` and handing off DRM master via logind session-activation, not VTs.
2. **Same-user policy**: does it _refuse_ to start a session for a user who already has
   an active graphical session elsewhere? Only one DM does. This is the wyrm2 blocker.

Almost every DM fails axis 1 (VT/seat0-only) — that's the real scarcity, not axis 2.

## Summary

| DM                 | VT-less multi-seat | Per-seat greeter | Same-user veto | Wayland greeter        | Verdict for wyrm2                                 |
| ------------------ | ------------------ | ---------------- | -------------- | ---------------------- | ------------------------------------------------- |
| **GDM**            | ✅ yes             | ✅ yes           | ❌ **blocks**  | ✅ (gnome-shell)       | Works but vetoes same-user                        |
| **SDDM**           | ✅ yes             | ✅ yes           | ✅ none        | ⚠️ via compositor cmd  | Best candidate; verify Wayland-on-2nd-NVIDIA-seat |
| **LightDM**        | ✅ yes             | ✅ yes           | ✅ none        | ⚠️ weak (greeters X11) | Candidate; per-seat autologin dodges greeter      |
| **greetd**         | ❌ seat0 hardcoded | ❌ no            | ✅ none        | ✅ (any wl greeter)    | Cannot reach seat-game                            |
| ly / emptty / nodm | ❌ tty-only        | ❌ no            | ✅ none        | ❌ (tty/X)             | Single seat0 only                                 |

✅ = supports / no obstacle. "Same-user veto ✅ none" means it does **not** block — good.

## Per-DM findings

### GDM 49.2 — multi-seat ✅, but same-user veto ❌

- **Multi-seat**: `GdmLocalDisplayFactory` enumerates seats via logind `ListSeats`
  (`gdm-local-display-factory.c:1029`) and subscribes to `SeatNew`/`SeatRemoved` /
  seat property changes (`:1403`+). `ensure_display_for_seat()` spawns a greeter per
  graphical seat. This is why seat-game gets its own greeter today.
- **No per-seat exclude**: there is no config key to make GDM ignore a seat. It claims
  every seat with `CanGraphical=yes`. (Confirmed: no such key in `data/*.in` schemas.)
- **Same-user veto**: lives in the **gnome-shell greeter**, not the GDM daemon.
  `js/gdm/loginDialog.js` `_onSessionOpened` (`:1291`) calls `_findConflictingSession`,
  which matches _any_ active wayland/x11 logind session owned by the same user
  (regardless of seat), and if found opens `ConflictingSessionDialog` and returns
  without calling `StartSessionWhenReady`. `_CONFLICTING_SESSION_DIALOG_TIMEOUT = 60`
  coincides with GDM's own session-start timeout → "Session was cancelled". No gsetting
  or env disables it. Introduced for _remote_-session hijack protection (dialog text is
  all about remote vs local); the local multi-seat case is collateral.
- **Verdict**: the only DM that fails purely on axis 2. Everything else it does is right.

### SDDM — multi-seat ✅, no same-user veto ✅ (strongest candidate)

- **Multi-seat**: `SeatManager` calls `ListSeats` and connects `SeatNew`/`SeatRemoved`
  (`src/daemon/SeatManager.cpp:106,119,120`). `logindSeatAdded` → `createSeat(name)`
  for **every** graphical logind seat (`:63-68`), not just seat0.
- **Per-seat env**: `XDG_SEAT` is set to `seat()->name()` for both greeter and session
  (`Greeter.cpp:211`, `Display.cpp:458`) — not hardcoded.
- **VT-less on non-seat0**: `CanTTY` / VT is explicitly gated on seat0 —
  `Seat.cpp:144` returns TTY-capable only when `name == "seat0"`; `XDG_VTNR` is set only
  when `seat()->name() == "seat0"` (`Greeter.cpp:214`); the VT jump is guarded by
  `terminalId() > 0` (`Seat.cpp:125-128`). So non-seat0 seats run without touching VTs.
- **No same-user veto**: grep for "already logged"/"conflicting"/"already running"
  across `src/` returns nothing. SDDM will start a session for a user who is already
  logged in elsewhere.
- **Wayland greeter**: `WaylandDisplayServer` runs a compositor from
  `Wayland.CompositorCommand` (default weston) and runs `sddm-greeter` inside it
  (`Display.cpp:135-136`). **Risk**: that compositor must come up on the second NVIDIA
  seat — unverified here.
- **Verdict**: architecturally does exactly what's wanted. Practical unknown is only the
  Wayland-greeter compositor on a second NVIDIA seat. Per-seat **autologin** would skip
  the greeter compositor entirely and remove that risk.

### LightDM — multi-seat ✅, no same-user veto ✅ (candidate; weaker Wayland)

- **Multi-seat**: the multi-seat DM by lineage (Ubuntu thin-client era). Config supports
  `[Seat:*]`, `[Seat:seat0]`, `[Seat:seat-thin-client*]` glob sections
  (`data/lightdm.conf:44-86`). `login1.c` handles `SeatNew`/`SeatRemoved` (`:227,239`)
  and `CanGraphical`/`CanMultiSession` changes (`:201-203`); `lightdm.c`
  `add_login1_seat`/`update_login1_seat` (`:401`+) creates a Seat per graphical logind
  seat, reading `can_multi_session` / `can_tty`.
- **Per-seat env**: `XDG_SEAT` = `seat_get_name(seat)` (`seat.c:408`,
  `seat-local.c:229,238`) — per seat, not hardcoded.
- **No same-user veto**: no "already logged in / refuse" logic; the only "already
  active" hit (`lightdm.c:507`) is _session reuse/activation_, not a refusal.
- **Wayland**: `wayland-session.c` exists and `seat.c:974` defaults a session to wayland
  when a `wayland-sessions` dir is present. **But** `seat-local.c:191-197`
  (`create_wayland_session`) sets a VT on the session from `vt_get_active()`, and
  LightDM's _greeters_ (lightdm-gtk-greeter, slick-greeter, web-greeter) are X11. Its
  Wayland-greeter story is weaker than SDDM's.
- **Per-seat autologin**: `[Seat:seat-game]` with `autologin-user=` / `autologin-session=`
  is a first-class feature — this **skips the greeter** on that seat entirely (no
  compositor, no dialog), launching sway directly. This is LightDM's cleanest fit here.
- **Verdict**: viable, especially via per-seat autologin. Wayland _greeter_ is the weak
  spot; autologin sidesteps it.

### greetd — cannot reach seat-game ❌

- **Hardcoded seat0**: `greetd/src/session/worker.rs:216` puts the literal
  `"XDG_SEAT=seat0"` into the PAM env of every session it starts. No config override.
- **VT-based**: `greetd/src/terminal/mod.rs` drives sessions via `KDGRAPHICS`/`KDTEXT`
  ioctls on `/dev/ttyN`. seat-game has no VTs.
- **No same-user veto** (irrelevant — it can't target the seat at all).
- **Verdict**: deliberately minimal seat0/VT tool. Dead on arrival for non-seat0 seats.
  (It _is_ a perfect fit for seat0 itself, e.g. if pairing greetd-on-seat0 with a
  bespoke launcher on seat-game.)

### ly / emptty / nodm — single seat0/tty only ❌

Not source-verified in this pass (medium confidence, from their design): these are
minimal TUI/console greeters that run on a single `/dev/ttyN` on seat0. No logind
seat-following, no per-seat concept. Same-user is unrestricted, but they can't reach a
non-seat0 seat. Excluded.

## What this means for wyrm2 (seat-game)

The scarcity is axis 1 (VT-less multi-seat), and **three** DMs clear it: GDM, SDDM,
LightDM. GDM is disqualified _only_ by its gnome-shell same-user veto, which has no
per-seat exclude and no off switch short of patching gnome-shell. So the realistic
"same user, both seats, no dialog" paths are:

1. **SDDM**, replacing GDM for both seats. Greeter on seat0, per-seat handling for
   seat-game. Verify the Wayland greeter compositor starts on the second NVIDIA seat, or
   use seat-game autologin to skip it.
2. **LightDM**, replacing GDM for both seats, with `[Seat:seat-game]` **autologin** to
   sway (skips the greeter → skips every greeter-side risk) and a normal greeter (or
   autologin) on seat0.
3. **Patch GDM's gnome-shell greeter** — keep GDM, make `_findConflictingSession` skip
   different-seat sessions. Smallest behavioral change, but a gnome-shell rebuild to
   maintain across GNOME bumps.
4. **Different user** for seat-game — GDM as-is, zero code (out of scope for this report,
   which is about _same_-user).

Note the wyrm2 second session is **sway**, not GNOME, so the genuine per-user singleton
limit (one shared user bus / `systemd --user` per uid; GNOME claims fixed D-Bus names)
does **not** apply — GNOME-on-seat0 + sway-on-seat-game coexist for one user. See the
`programs.sway` comment in `nix/nixos/hosts/wyrm2/default.nix` and
<direct_display_bringup.md>.

## Open / unverified

- **SDDM & LightDM Wayland greeter on a second NVIDIA seat**: neither verified live. The
  compositor (SDDM: `Wayland.CompositorCommand`; LightDM: weak) must acquire DRM master
  on card0 as a `greeter`-class session on seat-game. Per-seat **autologin** avoids this
  entirely and is the lower-risk route for both.
- **Migration cost**: moving off GDM means re-homing the seat0 SPICE GNOME session under
  the new DM (session launch is straightforward; the seat0 greeter rendering on QXL/virtio
  is not NVIDIA and should be easy).
- ly/emptty/nodm not source-checked (excluded on design; low stakes).
