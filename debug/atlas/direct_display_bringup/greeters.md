# Greeter multi-seat capabilities — research report

**Question:** which display managers / greeters can run **two simultaneous graphical
sessions for the same user on the same machine**, one per logind seat (specifically:
seat0 = SPICE virtual display, seat-game = physical RTX 5090 + keyboard on wyrm2)?

**Date:** 2026-07-11. **Method:** source reading. Cloned into `/code/github.com/`:
GDM 49.2 (`GNOME/gdm`), gnome-shell 49.4 (`GNOME/gnome-shell`), greetd
(`kennylevinsen/greetd`), LightDM (`canonical/lightdm`), SDDM (`sddm/sddm`), systemd
v258 (`systemd/systemd`). Line numbers below are from those checkouts.

> **Update 2026-07-17 (live-confirmed) — this report had a blind spot.** The SDDM rows
> were read from the **`develop`** checkout, which already contains commit `cda8d93`
> ("Allow non-root greeters and sessions to start on kernels without VTs"). **nixpkgs
> ships the `v0.21.0` release, which predates that commit** and fails a criterion this
> report never checked — **Axis 1b** below. That gap is exactly why the seatphysical
> greeter stays black on wyrm2 today. Root cause confirmed live via the logind varlink
> wire log; full trace in <README.md>. Rows updated accordingly.

## The independent axes a DM must satisfy

The problem decomposes into three independent things a DM must satisfy:

1. **VT-less multi-seat**: can it spawn a greeter/session on a _non-seat0_ logind seat?
   Non-seat0 seats have no VTs (`CAN_TTY=0`) — see <README.md> for the
   VT/seat relationship. A DM whose session model is "allocate a VT, `chvt` to it"
   cannot drive such a seat. This requires following logind's `ListSeats` / `SeatNew` /
   `SeatRemoved` and handing off DRM master via logind session-activation, not VTs.
1. **(1b) No VC tty on a non-seat0 seat** — _newly discovered 2026-07-17, was not in the
   original checklist._ Even a DM that clears axis 1 can still fail here. systemd 258
   moved logind `CreateSession` to **varlink** and tightened validation: a greeter that
   sends a **virtual-console TTY** (`/dev/tty0`) for a seat with no VTs is rejected with
   `org.varlink.service.InvalidParameter{parameter:"Seat"}` → no session → no
   `XDG_RUNTIME_DIR` → greeter compositor aborts → **black screen**. The DM must send
   **no** `TTY`/`VTNr` off seat0. Shipped **SDDM 0.21.0 fails exactly here**
   (`src/helper/backend/PamBackend.cpp:255` sets `PAM_TTY=/dev/tty<XDG_VTNR>`
   _unconditionally_ → `tty0` when `XDG_VTNR` is unset). Upstream `cda8d93` guards it;
   unreleased. Confirmed live — see <README.md>.
1. **Same-user policy**: does it _refuse_ to start a session for a user who already has
   an active graphical session elsewhere? Only one DM (GDM) does. This is the original
   wyrm2 blocker that ruled GDM out.

Almost every DM fails axis 1 (VT/seat0-only) — that's the real scarcity. Axis 1b is a
newly-found trap that bites even axis-1-capable DMs on **systemd ≥ 258**.

## Summary

| DM                            | Axis 1: VT-less multi-seat | Axis 1b: no VC-tty (systemd ≥258) | Same-user veto | Wayland greeter        | Verdict for wyrm2                                      |
| ----------------------------- | -------------------------- | --------------------------------- | -------------- | ---------------------- | ------------------------------------------------------ |
| **SDDM `v0.21.0` (shipped)**  | ✅ yes                     | ❌ **sends `tty0`** (PamBackend)  | ✅ none        | ⚠️ via compositor cmd  | **Blocked today** — this is what runs; needs `cda8d93` |
| **SDDM `develop`/+`cda8d93`** | ✅ yes                     | ✅ guarded                        | ✅ none        | ⚠️ via compositor cmd  | Fixes 1b; unreleased → backport patch or pin           |
| **plasma-login-manager**      | ✅ yes (SDDM code)         | ✅ **guarded (verified)**         | ✅ none        | ✅ (kwin greeter)      | Fix IS released (v6.7.x); but not in 25.11 + KDE deps  |
| **GDM**                       | ✅ yes                     | ✅ (sets VTNr only on seat0)      | ❌ **blocks**  | ✅ (gnome-shell)       | Excluded: vetoes same-user (+user said no GDM)         |
| **LightDM**                   | ✅ yes                     | ⚠️ unverified (X11-centric)       | ✅ none        | ⚠️ weak (greeters X11) | Wayland multiseat effectively X11-only; weak fit       |
| **greetd** (+cage/ReGreet)    | ❌ seat0 hardcoded         | n/a (can't reach seat)            | ✅ none        | ✅ (any wl greeter)    | `XDG_SEAT=seat0` hardcoded + VT-driven → can't target  |
| ly / emptty / nodm            | ❌ tty-only                | n/a                               | ✅ none        | ❌ (tty/X)             | Single seat0 only                                      |

✅ = supports / no obstacle. "Same-user veto ✅ none" means it does **not** block — good.
Axis 1b added 2026-07-17; **it is the criterion that flips the shipped SDDM from "best
candidate" to "blocked until patched."**

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

### SDDM — axis 1 ✅, no same-user veto ✅, but **shipped release fails axis 1b** ❌

**Read the version distinction carefully — it is the whole story.** The bullets below
citing `Seat.cpp:144` (`canTTY()`) and the `terminalId() > 0` guard are from a
**`develop`** checkout; those symbols are `cda8d93` additions and **do not exist in the
`v0.21.0` release nixpkgs ships**.

- **Multi-seat (axis 1, both versions)**: `SeatManager` calls `ListSeats` and connects
  `SeatNew`/`SeatRemoved` (`src/daemon/SeatManager.cpp:106,119,120`); `logindSeatAdded`
  → `createSeat(name)` for **every** graphical logind seat (`:63-68`), not just seat0.
- **Per-seat env (both)**: `XDG_SEAT` = `seat()->name()` for greeter and session
  (`Greeter.cpp:211`, `Display.cpp:458`) — not hardcoded. `XDG_VTNR` is set only for
  seat0 (`Greeter.cpp:214`), so it is correctly absent off seat0.
- **Axis 1b — the regression, `v0.21.0` ONLY**: despite `XDG_VTNR` being absent off
  seat0, `src/helper/backend/PamBackend.cpp:255` computes
  `VirtualTerminal::path(sessionEnv.value("XDG_VTNR").toInt())` = `path(0)` =
  **`/dev/tty0`** and sets it as `PAM_TTY` **unconditionally**. pam_systemd forwards
  that VC tty to logind → systemd-258 varlink rejects it (`InvalidParameter{Seat}`).
  **This is the live wyrm2 blocker.** `develop` fixes it in `cda8d93` by wrapping the
  set in `if (sessionEnv.contains("XDG_VTNR"))` (plus a logind `CanTTY` check in the new
  `Seat::canTTY()`). Confirmed live 2026-07-17 (see <README.md>).
- **No same-user veto (both)**: grep for "already logged"/"conflicting"/"already
  running" across `src/` returns nothing — starts a session for an already-logged-in
  user.
- **Wayland greeter (both)**: `WaylandDisplayServer` runs a compositor from
  `Wayland.CompositorCommand` (default weston) and runs `sddm-greeter` inside it
  (`Display.cpp:135-136`). On wyrm2 the seat0 greeter renders fine on virtio; the
  physical-seat compositor never gets that far today (dies at axis 1b).
- **Release health ⚠️**: `v0.21.0` (2024-03) is the **newest tag**; `cda8d93` has sat
  on `develop` **since 2024-01** and never shipped. Betting on SDDM means either
  carrying a backport patch or pinning an unreleased snapshot indefinitely — a real
  maintenance smell, though the patch itself is tiny and upstream-authored.
- **Verdict**: architecturally the best fit, but the **shipped release is blocked on
  axis 1b**. To use SDDM we must apply `cda8d93` (backport patch on 0.21.0) or pin
  `develop`. See "Ways to run a fixed SDDM" below.

#### Ways to run a fixed SDDM (if we stay on SDDM)

1. **Backport `cda8d93` as a nixpkgs overlay patch on 0.21.0 (recommended).** Minimal,
   coherent, upstream-authored; smallest surface to audit. The `PamBackend.cpp` guard
   alone is the load-bearing hunk.
2. **Pin `sddm` to a `develop` snapshot / specific commit.** Gets `cda8d93` plus other
   post-0.21 Wayland-multiseat work, but ships unreleased code we own the pin for.
3. **`scamiran149/sddm-multiseat-wayland` fork** — a heavyweight (~1600-commit) fork
   purpose-built for Wayland-on-N-seats: per-seat `/run/sddm/<seat>` runtime dirs,
   non-root per-greeter D-Bus bus, and **deterministic per-seat `WAYLAND_DISPLAY`
   sockets** (`--socket wayland-seatX`). Not upstreamable, big divergence — but its
   socket-isolation work flags a **likely next problem after axis 1b**: two greeters as
   the `sddm` user can collide on `wayland-0` in `/run/user/<uid>/`. Treat as a source
   of _targeted_ extra patches, not a wholesale adoption.

### plasma-login-manager (PLM) — the KDE SDDM fork: clears every axis, but not in our nixpkgs yet

KDE forked SDDM in 2023 into **plasma-login-manager** (`KDE/plasma-login-manager`), the
default DM for Plasma 6.x. It is the answer to "why did SDDM stop releasing" — KDE's
forward energy went here. Checked from source (`master`, pushed 2026-07-16) + nixpkgs:

- **Axis 1 (multi-seat)**: inherits SDDM's `SeatManager` (same `ListSeats` /
  `SeatNew`/`SeatRemoved` per-seat greeter model). No divergence found — it is SDDM's
  multiseat code, not a KDE-special reimplementation.
- **Axis 1b (no VC tty) — PASSES, verified**: `src/helper/backend/PamBackend.cpp:255`
  has the guard `if (sessionEnv.contains("XDG_VTNR")) { setItem(PAM_TTY, …) }`, and
  `src/daemon/Seat.cpp:150` `Seat::canTTY()` queries logind's `CanTTY` property. i.e. it
  already carries `cda8d93` — and unlike SDDM, **it is in released code** (tags
  `v6.6.x … v6.7.3`, cut with Plasma).
- **Same-user veto**: none (inherited from SDDM).
- **Wayland greeter**: Plasma greeter runs under **kwin_wayland** (heavier than SDDM's
  weston). Sessions are session-agnostic — it launches whatever is in
  `wayland-sessions/`, so **sway on both seats is fine**; adopting PLM does **not** force
  a Plasma _session_.
- **Release cadence — SOLVED**: real, frequent releases (`v6.7.3` etc.) tracking Plasma.
  This is the one thing SDDM fails and PLM fixes.
- **nixpkgs — the catch**: option `services.displayManager.plasma-login-manager.enable`
  exists, but **only in NixOS ≥ 26.05**. wyrm2 is on **25.11**
  (`25.11.20260630`) and `pkgs.kdePackages.plasma-login-manager` evaluates to **MISSING**
  there. Using PLM needs a **system-wide nixpkgs bump to 26.05+** (or a package+module
  backport — heavier than a one-package overlay because it's a NixOS module).
- **KDE-dependency weight**: the greeter drags in KDE/Plasma greeter libs (kirigami,
  plasma-framework) and **kwin** as the greeter compositor — real closure growth on a
  box whose sessions are sway. The _sessions_ stay non-KDE; the _greeter_ is KDE.
- **Unverified**: live multiseat on PLM (same wayland-socket-collision question as SDDM —
  though kwin may handle per-seat sockets differently than weston); NVIDIA-seat kwin
  greeter bring-up.
- **Verdict**: architecturally the **cleanest "has-the-fix, actually-released" option**,
  and answers the cadence objection directly. Costs: a nixpkgs 26.05 bump (system-wide,
  not trivial) **and** KDE greeter deps. Best pick _if_ we're willing to bump nixpkgs;
  otherwise patched-SDDM is lighter and stays on 25.11.

### On the user's two questions (2026-07-17)

- **"Does KDE support this better?"** Partly. PLM (KDE's fork) has the axis-1b fix **in
  released code** and is actively maintained — so yes, the KDE line ships the fix SDDM
  won't. But the multiseat capability itself is **inherited SDDM code**, not a superior
  KDE reimplementation; KDE didn't make non-seat0 multiseat first-class, they just keep
  releasing. No evidence anyone treats VT-less multiseat as a blessed, tested scenario.
- **"Would this work with a non-KDE DM / non-KDE setup?"** Two senses:
  - _Non-KDE sessions under a KDE DM_: yes — PLM launches sway fine (session-agnostic).
  - _Avoiding KDE entirely_: then PLM is out (its greeter IS KDE). The genuinely non-KDE
    "has-the-fix" paths are **patched-SDDM** (weston greeter, no KDE, stays on 25.11) or a
    **bespoke greetd/cage** greeter. So the KDE-vs-not choice maps cleanly onto
    PLM-vs-patched-SDDM.

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

## What this means for wyrm2 (2026-07-17 — updated for current state + constraints)

**State now:** already migrated to SDDM for both seats; **seat0 is on sway** (works, incl.
swaylock — resolved the GNOME-lock-needs-GDM problem). The physical seat (`seatphysical`)
is the only thing left, and it is **blocked purely on axis 1b** (shipped SDDM 0.21.0
sends `tty0`). Not a hardware, DRM, or KVM issue — confirmed live.

**Constraint change since the original report:** the user has ruled out **autologin on
the physical seat** (it must be a real login gate), and **no GDM**, **no separate user**.
⚠️ **This kills the "per-seat autologin dodges the greeter" escape hatch** that the SDDM
and LightDM verdicts above leaned on. We now genuinely need a **working greeter** on a
non-seat0 seat — the hardest cell in the whole matrix.

Only two packaged DMs both clear every axis _and_ carry the 1b fix: patched-SDDM and
plasma-login-manager. The choice between them is essentially **KDE or not**:

1. **SDDM + `cda8d93` — lightest, non-KDE, stays on 25.11.** Backport patch on 0.21.0
   (or pin `develop`). No nixpkgs bump, no KDE deps, weston greeter. Cost: we carry a
   patch on a stale-release base. Fastest to try — we have the udev re-seat + logind-debug
   loop, so it validates with no reboot.
2. **plasma-login-manager — released & maintained, but heavier.** Has the 1b fix in
   _shipped_ code (answers the cadence objection) and launches sway fine. Cost: **needs a
   system-wide nixpkgs bump to 26.05+** (not in wyrm2's 25.11) **and** pulls KDE/kwin
   greeter deps. Best if we're bumping nixpkgs anyway or want off the patch-carrying
   treadmill.
3. **LightDM — weak.** Clears axis 1, but Wayland-greeter story is X11-centric
   ("disable Wayland" per its own multiseat guidance); its autologin dodge is now
   disallowed. Only if both above fail.
4. **greetd / ly / emptty — out.** greetd hardcodes `XDG_SEAT=seat0` and is VT-driven;
   the others are seat0/tty-only. None can target `seatphysical`.
5. **GDM — out** (same-user veto + user said no GDM).
6. **Bespoke `cage`+greeter unit on `seatphysical`** — not yet researched; the only
   "roll our own" path if every packaged DM fails. Would need its own logind session
   without a VC tty. Last resort; add findings here if pursued.

**Decision pending (user):** patched-SDDM (fast, light, non-KDE, carry a patch) vs
plasma-login-manager (released, maintained, but nixpkgs-26.05 bump + KDE deps).

## Open / unverified

- **Does SDDM + `cda8d93` fully bring up the physical greeter?** Axis 1b is fixed by the
  patch, but the next unknown is whether the two greeter compositors (both as the `sddm`
  user) collide on `WAYLAND_DISPLAY`/`/run/user/<uid>/wayland-0` — the exact problem the
  `scamiran149` fork isolates with per-seat sockets. **This is the first thing to check
  after patching.**
- **Bespoke cage+greetd/gtkgreet on a non-seat0 seat**: not source-verified. greetd is
  seat0/VT-bound (above); whether `cage` can be launched directly on `seatphysical`
  (bound via `XDG_SEAT`, no VC tty) as a login gate is unresearched. Only relevant if
  SDDM is abandoned.
- **LightDM Wayland greeter on a second NVIDIA seat**: never verified live; low priority
  now (deprioritized behind SDDM).
- ly/emptty/nodm not source-checked (excluded on design; low stakes).
