# Atlas Ethernet — Recurring Link Failures (Resolved)

Atlas's uplink — Aquantia AQC113CS 10G (`enp12s0`, atlantic), bridged into
`vmbr0`; the second onboard NIC (Intel I226-V, `enp11s0`, igc) is an unused
backup — flapped for a month, then lost link entirely on 2026-03-06. **Root
cause: the self-crimped cable.** Replaced 2026-03-07 with a factory Cat6a; link
came up immediately, stable since (~6 months as of 2026-08). Quick commands:
<CHEATSHEET.md>. Incident log and UniFi tables: git history of this file.

## Recognition signature

Connection durations shortening from days to hours over weeks = a dying
cable/connector. UniFi logged 75 disconnects over the final month, typical
connection duration degrading from 12+ days to single-digit hours before total
failure — a textbook progressive degradation curve.

## Diagnostic isolations that worked

- **PHY internal loopback PASS while external loopback FAILs** — fault is
  at/beyond the RJ45 connector; NIC hardware and firmware are fine.
- **Both NICs show "Link detected: no"** — physical layer (cable, connector, or
  switch port), not driver or config.
- **A driver reload does not power-cycle the PHY** — a clean, linkless
  `modprobe -r atlantic && modprobe atlantic` proves nothing about the cable.

## Non-cause

PCI BAR conflicts at boot (`can't claim; no compatible bridge window` on both
NICs, from 2x RTX 5090 address-space pressure) are benign here — they appear on
working boots too.

## Open next step

Adding GPUs once re-ordered PCI enumeration and renamed both NICs
(`enp10s0`/`enp11s0` → `enp11s0`/`enp12s0`), silently breaking the bridge
config. If that bites again: pin interface names by MAC via udev rules and
update `/etc/network/interfaces` to match.
