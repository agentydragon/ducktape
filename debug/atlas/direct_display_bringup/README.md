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

## History (archived)

The road here was long and cursed — gamescope kiosk → sway seats → SDDM → PLM →
greetd, and finally the discovery that GDM can't do a non-`seat0` user login at
all. Kept for the reasoning trail, not as current plans:

- <archive/2026_07_multiseat_saga.md> — full chronological bring-up log.
- <archive/greeters.md> — grounded display-manager capability matrix (which DMs
  can drive a non-seat0 seat, the VC-tty/systemd-258 trap, same-user vetoes — and
  why none of them cleanly works here).
- <archive/plm-mr155-per-seat-greeter.patch> — shelved PLM per-seat greeter
  backport, the fallback _if_ two simultaneous local seats ever become a real
  requirement.
