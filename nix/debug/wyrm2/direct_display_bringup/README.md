# wyrm2 display: single-seat0 + remote

Current, working design (2026-07-17). The display 5090 is wyrm2's **only** local
graphical seat; remote access is a **session, not a seat**. This supersedes a long,
cursed multiseat investigation — see [History](#history-archived).

## Design

- **Local (physical monitor).** The display 5090 (`01:00.0`, DP → FV43U) is
  `seat0`'s sole graphical output: a normal, fully-supported GDM `seat0` GNOME
  login. The virtio-gpu (`00:01.0`, SPICE) and the spare 5090 (`02:00.0`) are
  `mutter-device-ignore`'d, so mutter renders only on the 5090. virtio stays a
  plain text/recovery console.
- **Remote.** `gnome-remote-desktop` **Remote Login** (headless system RDP with
  GDM handover) → a fresh headless GNOME session on connect, no prior local login
  needed, virtual monitor resizes to the client. Reached over an **SSH-key tunnel
  on nebula** (`ssh -L 3389:localhost:3389 <wyrm2-nebula>`, then RDP to
  `localhost:3389`); RDP is not firewalled open, so the SSH key is the gate.
- **Why single-seat0 (not multiseat).** GDM **cannot** complete a user login on a
  non-`seat0` seat — the multiseat Wayland handoff (gdm!291) is unmerged, blocked
  on [systemd#42247](https://github.com/systemd/systemd/issues/42247) — and we
  don't need two _simultaneous local_ graphical seats (physical vs. remote are
  never both live). So we collapsed to one seat and remote in. Full reasoning +
  the display-manager capability matrix: [History](#history-archived).

## Wiring

`nix/nixos/hosts/wyrm2/default.nix`:

- `services.udev.extraRules` — `mutter-device-ignore` on virtio + spare 5090.
- `services.displayManager.defaultSession = "gnome"` (mutter honours the
  ignore-tag; wlroots/sway does not — so sway on `seat0` would grab all 3 GPUs).
- `services.gnome.gnome-remote-desktop.enable` + the `grd-system-rdp-setup`
  one-shot (self-signed TLS, chowned to the `gnome-remote-desktop` user,
  `grdctl --system rdp enable`).
- `services.displayManager.gdm.debug = true` — tombstoned; without it a
  greeter→session failure is a silent freeze.

## Deploy

```bash
sudo nixos-rebuild switch --flake ~/code/ducktape#wyrm2
```

### Applying seat changes without a full reboot

udev seat tags (`ID_SEAT`) persist in `/run/udev/data` **and** in logind's
runtime, so a `switch` alone does not move a card between seats. To apply live
(verified 2026-07-17 — collapsed `seatphysical`/`seatspare` back into `seat0`
with no reboot and no logind restart):

```bash
sudo rm -f /run/udev/data/c226:2                                     # spare card's stale ID_SEAT=seatspare
sudo udevadm control --reload
sudo udevadm trigger --action=add    /sys/class/drm/card0 /sys/class/drm/card2
sudo udevadm trigger --action=change /sys/class/drm/card0 /sys/class/drm/card2   # nudge logind to re-read
loginctl list-seats                                                  # expect: only seat0
sudo systemctl restart display-manager                              # fresh seat0 greeter on the 5090
```

Why this works: the `switch` already stripped `ID_SEAT` from `card0` (it just
needed logind to re-evaluate), and only the spare card still carried a persisted
`ID_SEAT=seatspare`. Deleting that db entry + `change` events made logind drop
both stale seats. (A reboot does the same thing deterministically; this is the
no-reboot path.)

## Recovery / diagnostics

- **Login succeeds but the desktop never appears / wedges** → <login*zombie_recovery.md>
  (a zombie logind session or a stuck `graphical-session.target`; note that
  restarting the display manager does \_not* fix either).
- **Seat/DRM/logind state** → `sudo bash seat-diag.sh` (DM-agnostic).

## History (archived) — the constraints we ran into

The road here was long and cursed — gamescope kiosk → sway seats → SDDM → PLM →
greetd — and ended at the finding that **no mainstream DM cleanly drives a
non-`seat0` Wayland seat today** (source-grounded 2026-07-17). The constraints,
compactly; each cost real investigation and still binds if anyone retries:

- **Non-seat0 seats have no VTs** (`CAN_TTY=0`) — any DM whose session model is
  "allocate a VT and `chvt`" structurally cannot drive one. That scarcity alone
  kills ly/emptty/nodm and most of the field.
- **systemd ≥ 258 varlink `CreateSession` trap**: a greeter that sends a
  virtual-console tty (`/dev/tty0`) for a non-seat0 seat is rejected with
  `InvalidParameter{Seat}` (`logind-varlink.c` seat/VC check) → no session → no
  `XDG_RUNTIME_DIR` → the greeter compositor aborts to a black screen.
- **Shipped SDDM 0.21.0 hits exactly that**: `PamBackend.cpp` sets
  `PAM_TTY=/dev/tty<XDG_VTNR>` unconditionally (`tty0` when unset); guarded
  only in unreleased upstream `cda8d93`.
- **GDM**: the non-seat0 _greeter_ works (gdm!174, merged), but a non-seat0
  _user login_ opens the PAM session and then never launches a compositor
  (`Session never registered, failing`) — gdm!291 unmerged, blocked on
  systemd#42247. GDM is also the only DM with a same-user veto (refuses a
  session for a user already graphically active elsewhere), the original
  blocker.
- **plasma-login-manager** clears the session axes, but its greeter is
  single-instance (fixed-name user units) — a second seat stays black without
  the unmerged MR 155 per-seat greeter (shelved backport in git history).
- **greetd** hardcodes `XDG_SEAT=seat0` and is VT-driven — cannot target
  another seat at all.
- **Two identical GPUs are software-indistinguishable to Vulkan**: DXVK/NVIDIA
  always picks the first-PCI device; gamescope's `--prefer-vk-device` takes
  only `vendor:device` (identical for both 5090s) and NVIDIA's ICD honors no
  per-app PCI filter. There is no software way to pin a game to one of two
  identical cards — the fix was physical: move the monitor to the GPU games
  render on (`01:00.0`).
- **Cross-seat DRM opens fail hard**: a session that opens a card owned by
  another seat gets a logind `TakeDevice` denial → gamescope
  `Aborted (core dumped)` → wedged greeter (why the leftover kiosk session
  entries had to be removed, not just unused).
- **NVIDIA DP/HDMI audio sinks default to 10000% volume** in PipeWire (it
  allows > 100%) — clamp (`wpctl set-volume … 0.3`; waybar capped at
  `max-volume=100`).
- **FV43U KVM auto-reverts its uplink binding to USB-C** — during USB-B
  testing the hub silently never leaves USB-C; use the OSD Input menu to
  isolate video from USB.

The full chronological bring-up log and the grounded per-DM capability matrix
behind these are in git history (`archive/`, removed 2026-08).
