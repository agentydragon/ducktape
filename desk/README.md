# Desk wiring

Working notes for the home desk setup. Goal: hang every peripheral off
a Thunderbolt KVM so one button-press swaps the whole desk between
**atlas** (see top-level <../README.md>) and a hot-plug laptop.

Living doc: inventory, target topology, cable plan, mounting TODOs,
cable routing, open questions. Chronological experiments and
hypothesis-update history live in <debug/build_log.md>.

**North star** — **achieved 2026-07-01**: one-button switching
between atlas (desktop) and whatever laptop is docked, both plugged
into the SB-TB4K with a single USB-C cable each; everything else
(monitor, keyboard, camera, underdesk hub) downstream of the KVM.
Plan A in "Target topology" describes the wiring.

## Desired state

Detailed spec the desk needs to satisfy — the "one-switch KVM" as
intended:

- **Two hosts on the SB-TB4K.**
  - `atlas` — permanent, occupies one host port.
  - **Laptop slot** — variable. `rugged` is currently sitting in it,
    but the slot is designed as a rotating dock point for whatever
    laptop is at the desk at a given time.
- **One KVM button press switches all shared peripherals** between
  the two hosts, atomically:
  - AORUS FV43U monitor (video + audio).
  - The FV43U's own built-in USB hub — reaches the active host back
    through the monitor USB-C uplink → KVM downstream.
  - USB-A camera on the FV43U's lower downstream USB-A port (rides
    the monitor hub).
  - TEX Shura keyboard — rides the monitor hub too (upper USB-A
    downstream), so a single upstream cable brings the keyboard
    with the KVM switch instead of an additional KVM USB-A run.
  - Underdesk USB-A hub on a KVM USB-A port.
- ~~**Both host ports receive PD continuously**~~ **Not achievable
  with the SB-TB4K.** Per Sabrent's own forum, delivering PD to
  both host ports simultaneously would violate Thunderbolt 4
  certification, so the switch only charges the currently-active
  host by design. See the gap discussion in the North-Star
  experiment. Workaround: if the docked laptop needs to charge
  while atlas is the active host, plug its own AC adapter alongside
  the KVM cable.

### Future desiderata (not blocking Plan A)

- **Play games with the RTX 5090s (wired 2026-07-05).** Direct
  output: RTX 5090 `01:00.0` DP-OUT → FV43U DP 1.4 in (Ivanky 8K DP
  m-m); keyboard via FV43U USB-B uplink → atlas rear USB-A (bus 3 port 2)
  → QEMU port-path passthrough to wyrm2 at path `3-2.1` (USB A→B). Monitor dual-KVM OSD
  configured USB-B ↔ DP and USB-C ↔ USB-C — one press switches both
  video and USB hub between the host/KVM path (USB-C) and wyrm2's local
  `seat0` (DP/USB-B path). wyrm2 runs GNOME on that physical seat; per-title
  `gamescope -f -- %command%` handles direct scanout per game. DP
  audio via `01:00.1` passthrough (PipeWire → FV43U speakers).
  Sunshine/Moonlight streaming available but software-encode-only.
  Session-teardown hardening is the remaining open item. See
  `debug/atlas/direct_display_bringup/README.md`.

## Devices on hand

| Device              | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| atlas               | Proxmox host. iGPU → mobo TB4-OUT → KVM → FV43U USB-C (Proxmox console + seat0 SPICE). Both RTX 5090s VFIO-bound to wyrm2 VM via VFIO: host `01:00.0` (ZOTAC, IOMMU group 14) → hostpci0 → guest `01:00.0` = display GPU (DP-OUT → FV43U DP 1.4 in, DRM connector `card0-DP-1`; whole device `0000:01:00` passed so audio function `01:00.1` comes with it for DP audio); host `03:00.0` (Gigabyte, IOMMU group 16) → hostpci1 → guest `02:00.0` = headless compute. GPU DP-OUT → mobo DP-IN loopback removed 2026-07-01. |
| AORUS FV43U         | 43" 4K@144 monitor. Inputs: 1× DP 1.4, 2× HDMI 2.1 (24 Gb/s), 1× USB-C (DP-Alt + USB data + PD). USB hub: 1× USB-B uplink, 2× USB-A downstream. 2× 3.5 mm jacks (headphone, line-out). Internal "dual-KVM" toggles which uplink (USB-B vs. USB-C) feeds the hub. Hub chip: Realtek RTS5411 (USB VID `0bda:5411`).                                                                                                                                                                                                         |
| Sabrent SB-TB4K     | TB4 KVM. 2× TB4 host (PC1, PC2) + 3× TB4 downstream (40 Gb/s, 60 W PD per port) + 4× USB-A 3.2 Gen 2 (10 Gb/s, 5 V / 2.4 A). **No standalone DP output** — video goes over TB4 downstream USB-C. USB VID `2eb9:0123` (SSI TBT4 KVM HUB).                                                                                                                                                                                                                                                                                  |
| TEX Shura           | 60% mech with trackpoint, USB-C jack at the back. USB VID/PID `04d9:0532` (Holtek). One unit; lives on FV43U USB-A upper port permanently. The FV43U dual-KVM routes the hub to USB-C (host path: Sabrent KVM → atlas/laptop) or USB-B (wyrm2 local `seat0` path: atlas rear USB-A bus 3 port 2 → QEMU at `3-2.1` → wyrm2). Keyboard follows the monitor switch — no manual replug needed.                                                                                                                                |
| USB-A camera        | Logitech C920 HD Pro. Sits atop the FV43U, plugged into the monitor's **lower** USB-A downstream port. USB-A plug on the camera end.                                                                                                                                                                                                                                                                                                                                                                                      |
| Underdesk USB-A hub | Mounted left-underside of the desk. USB-A uplink plug — plugs directly into a KVM USB-A port.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| USB WiFi adapter    | Model TBD. Spare; could go into atlas as a temporary wireless NIC.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

## Cables on hand

Cables physically present at home. Anything in the storage unit is
listed in the next section so we don't accidentally plan around it.

| Qty | Cable                               | Vendor   | Current state                                                                                                 |
| --- | ----------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| 1   | DP male-male                        | —        | Spare. Was the GPU DP-OUT → mobo DP-IN loopback until removed 2026-07-01.                                     |
| 1   | DP male-male, 8K                    | Ivanky   | In use: RTX 5090 `01:00.0` DP-OUT → FV43U DP 1.4 in (gaming display path).                                    |
| 1   | USB A → USB B                       | —        | In use: FV43U USB-B uplink → atlas rear USB-A (bus 3 port 2 → QEMU port-path passthrough → wyrm2).            |
| 1   | USB-C, 40 Gb/s, 200 W (TB-class)    | Silkland | In use: KVM downstream TB4 → FV43U USB-C (video + hub + PD).                                                  |
| 1   | USB-C, USB 3.2 Gen 2×2, 20 Gb/s, 8K | —        | In use: laptop-side host link (rugged ↔ SB-TB4K PC2). Visibly worn on one connector but working in this role. |
| 1   | Shielded Ethernet, ~1 ft            | —        | Spare. Known-good but almost certainly too short to reach atlas from the wall jack.                           |
| 1   | USB-A extension (female ↔ male)     | —        | Previously ran to the monitor; repositionable.                                                                |
| 1   | USB-A → USB-C                       | —        | Tagged green, label "keyboard". Spare.                                                                        |
| 1   | USB-A → USB-C                       | —        | Untagged. Currently in use with the TEX Shura.                                                                |
| 1   | USB-A → USB-C                       | —        | Spare.                                                                                                        |
| 1   | DP ↔ USB-C                          | Ivanky   | Spare. Passive DP-Alt adapter cable.                                                                          |
| 1   | Thunderbolt USB-C, ~2 ft            | Sabrent? | Spare. Tagged "4". Likely a TB4 cable bundled with the SB-TB4K. Verify the marking.                           |

## In storage (not available now)

- Colored cable-marker paper rings (see "Cable marking" below).

## Target topology

Plan A (USB-C only) is what's currently wired — the monitor link is a
single USB-C cable carrying DP-Alt video + USB hub data + PD, which
lets the camera ride the FV43U's own USB-A hub back up to the
KVM-switched bus. Plan B (DP video + a separate USB A→B hub uplink)
is a fallback, described after; the cables for it are on hand either
way.

The physical schematic — devices, ports, cables, grommets, colour
map — lives in <wiring_schematic.svg> (source: <wiring_schematic.dot>).

Free after Plan A wiring: SB-TB4K downstream TB4 ports B and C,
SB-TB4K USB-A 2 / 3 / 4, FV43U 2× HDMI 2.1. FV43U DP 1.4 and
USB-B uplink are consumed by the gaming display path (Plan A rev 2).

### Port-assignment caveats

- **atlas RTX 5090 DP-OUT slot** — atlas has 2× RTX 5090. Which card
  and which DP port feeds the internal DP m-m to the mobo isn't
  recorded yet.
- **SB-TB4K downstream port choice (A vs. B vs. C)** — they're
  symmetric; any one can carry the monitor video. Pick by cable reach.
- **Which SB-TB4K USB-A number** each accessory ends up on is drawn
  arbitrarily; label the physical port once cables are pulled.

## Cable plan vs. inventory (Plan A)

Each row is one physical link.

| Link                     | Source port                                | Destination port                  | Cable          | Have?                                                                                                                                                                                         |
| ------------------------ | ------------------------------------------ | --------------------------------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| atlas internal video     | atlas RTX 5090 DP-OUT (slot ?)             | atlas mobo DP-IN                  | DP m-m         | Removed 2026-07-01; display comes from the iGPU. Not needed for the gaming plan (Sunshine streaming — see `debug/atlas/gpu-strategy.md`).                                                     |
| atlas → KVM              | atlas mobo TB4-OUT (**middle** port)       | SB-TB4K host PC1                  | USB-C TB-class | Cabled — Sabrent-looking 2 ft, tag "4". Atlas's top TB port carries BIOS video but not kernel DP-Alt; middle port works after kernel takeover, so the cable belongs there.                    |
| Laptop → KVM             | Laptop TB4 (USB-C) — right-drawer position | SB-TB4K host PC2                  | USB-C          | In use — 20 Gb/s USB 3.2 Gen 2×2 cable, currently plugged into rugged as laptop stand-in. Not TB4, but the SB-TB4K accepts it and switches cleanly.                                           |
| KVM → monitor (combined) | SB-TB4K downstream TB4 (USB-C)             | FV43U USB-C in (video + hub + PD) | USB-C TB-class | In use — Silkland 40 Gb/s 200 W. Replaces the visibly damaged 20 Gb/s cable that was flaky under strain.                                                                                      |
| Monitor hub → keyboard   | FV43U USB-A downstream (upper)             | TEX Shura USB-C                   | USB-A → USB-C  | In use — the keyboard is on the monitor hub, not the KVM directly. Cable runs monitor → back-right grommet → under-desk → keyboard. Still switches with the KVM via the monitor USB-C uplink. |
| KVM → underdesk hub      | SB-TB4K USB-A                              | Hub uplink (USB-A plug)           | none (direct)  | Yes — hub plugs in. USB-A F↔M extension available if reach is short.                                                                                                                          |
| Monitor hub → camera     | FV43U USB-A downstream (lower)             | Camera (USB-A plug)               | none (direct)  | Yes — camera plugs in.                                                                                                                                                                        |

All links buildable with cables on hand. Remaining cable-side
questions: the tag-4 cable's TB4 marking (atlas host link) and the
underdesk USB-A reach.

### Plan B — fallback if Plan A's USB-C link won't hold

If the 20 Gb/s USB-C cable can't be made reliable (see
<debug/build_log.md>), swap the "KVM → monitor (combined)" row for
two links:

| Link                  | Source port                    | Destination port   | Cable      | Have?                        |
| --------------------- | ------------------------------ | ------------------ | ---------- | ---------------------------- |
| KVM → monitor (video) | SB-TB4K downstream TB4 (USB-C) | FV43U DP 1.4 in    | USB-C ↔ DP | Yes — Ivanky DP/USB-C cable. |
| KVM → monitor (hub)   | SB-TB4K USB-A                  | FV43U USB-B uplink | USB A → B  | Yes.                         |

Camera stays on the FV43U USB-A downstream either way. Consequences of
switching: burns one extra KVM USB-A port (the USB-B uplink); the
20 Gb/s USB-C cable and the FV43U USB-C input become spare.

### Gaming display path (Plan A rev 2 — wired 2026-07-05)

Direct DP output from wyrm2's display 5090 to the monitor. The
monitor's dual-KVM OSD (USB-B ↔ DP, USB-C ↔ USB-C) switches both
video input and hub uplink in one press, routing between the host/KVM
(USB-C) and wyrm2 local `seat0` (DP/USB-B) paths.

| Link                      | Source port               | Destination port                             | Cable      | Have?                                                                                 |
| ------------------------- | ------------------------- | -------------------------------------------- | ---------- | ------------------------------------------------------------------------------------- |
| wyrm2 display → monitor   | RTX 5090 `01:00.0` DP-OUT | FV43U DP 1.4 in                              | DP m-m, 8K | In use — Ivanky 8K DP m-m.                                                            |
| Monitor hub → wyrm2 input | FV43U USB-B uplink        | atlas rear USB-A bus 3 port 2 → QEMU → wyrm2 | USB A → B  | In use — keyboard at QEMU path `3-2.1` (hub child port 1) when monitor in USB-B mode. |

**Gotcha: the USB A→B cable must stay in the atlas port the passthrough
is pinned to.** wyrm2's `usb3:` entry pins a **physical host port path**
(`host=3-2.1`), not a VID/PID. Move the cable to another atlas USB-A
port and the symptom is: the KVM press still switches video to wyrm2,
but the keyboard silently lands on the **atlas host** instead — atlas's
`usbhid` binds it and QEMU, still watching the vacated port, hands the
guest nothing.

Pinning by port is deliberate, not an oversight: pinning by the Shura's
`04d9:0532` would make QEMU seize the keyboard on the USB-C path too,
stealing it from atlas in work mode. So the port path is load-bearing —
**re-pin it whenever the cable moves**:

```bash
# 1. With the monitor switched to USB-B, find where the hub landed:
ssh root@atlas 'dmesg -T | grep -E "idProduct=(0532|5411)" | tail -4'
# 2. Re-pin (config for next boot):
ssh root@atlas 'qm set 110 -usb3 host=<bus>-<port>.1,usb3=1'
# 3. Apply to the *running* VM — `qm set` only stages it (see below):
ssh root@atlas 'qm monitor 110 <<< "device_del usb3"'
ssh root@atlas 'qm monitor 110 <<< "device_add usb-host,bus=xhci.0,hostbus=<bus>,hostport=<port>.1,id=usb3"'
```

**Deviation from stock Proxmox:** despite `hotplug: network,disk,cpu,usb`,
`qm set -usbN` leaves the change in `qm pending` and does **not** reach
the running VM — verified 2026-09-04, including via delete-then-re-add.
The QEMU-monitor `device_del`/`device_add` pair above is the live path.

**Finding the port without switching the monitor:** the hub's SuperSpeed
half (`0bda:0411`) stays enumerated on atlas even while the monitor is in
USB-C mode, so `lsusb -t` shows it on bus 4 — and the kernel pairs the
buses 1:1 via `/sys/bus/usb/devices/usb4/4-0:1.0/usb4-portN/peer`, so
SS port N maps to the HS port the keyboard will use.

## Cable marking

Deferred until the marker paper comes out of storage. Sketch of options
to decide between then:

- Colored bands at each end, color = port group (host TB / video /
  USB-downstream).
- Numbered tags `01..NN` with the key recorded here.

## Cable routing

Physical paths cables take through / around the desk. Grows as more
cables are pulled.

- **KVM → monitor (Silkland USB-C)** — routed through the
  **back-right grommet hole** on the desk. The monitor end sits at
  the FV43U's USB-C input; the KVM end drops under the desk.
- **Monitor → keyboard (USB-A → USB-C)** — runs from the FV43U's
  **upper** USB-A downstream port down through the **back-right
  grommet hole** (same one the Silkland uses), under the desk, and
  up to the TEX Shura in the keyboard slot.
- **KVM → laptop slot (20 Gb/s USB-C)** — cable runs from the
  SB-TB4K under the desk, up through the **right-drawer grommet
  hole**, and into whichever laptop is docked (currently rugged).
  Cable is deliberately oriented with the visibly-worn end at the
  laptop side (visible + easy to grab) and the clean end at the KVM
  (buried under the desk), so if it acts up we can inspect / replug
  the suspect connector fast.
- **5090 → monitor DP (Ivanky 8K DP m-m)** and **monitor USB-B →
  atlas USB-A (USB A→B)** — routing not yet recorded.

## Open questions

- Whether the "tag 4" ~2 ft Sabrent-looking USB-C cable is actually
  TB4-marked (now the atlas host link — cabled, marking not yet
  visually verified).
- Which SB-TB4K host port (PC1 vs. PC2) each host cable is landed on.
- Physical placement of the KVM (desktop vs. underdesk mount) —
  affects all USB-A cable lengths.
- Per-link cable-length constraints (desk-edge to under-desk, monitor
  back to KVM, KVM to camera position, etc.). Measure as we go.
- Does the FV43U dual-KVM binding (USB-B ↔ DP, USB-C ↔ USB-C)
  survive monitor power cycles?
- Routing details for the gaming-path cables (DP m-m + USB A→B) — not
  yet recorded.

## Networking (separate from KVM plan)

atlas's network link isn't part of the KVM topology, but the inventory
is being gathered together. Current options if the in-use Ethernet path
doesn't work:

- Use the on-hand shielded Ethernet cable if reach permits (it's short).
- Drop the USB WiFi adapter into atlas as a stopgap wireless NIC.

## Placement & mounting

Current physical state and mounting TODOs.

- **SB-TB4K KVM box.** Sitting on top of the atlas tower for now — a
  temporary perch, not a good long-term spot.
  - **TODO:** mount it to the desk (or the under-desk grid).
    Candidate: magnetic tape on a ferromagnetic surface, otherwise
    Velcro / 3M-Command style adhesive.
- **KVM toggle switch (wired remote button).** Routed to sit next to
  the keyboard so it's within thumb reach.
  - **TODO:** mount it. The toggle body is ferromagnetic, so magnetic
    tape is the leading candidate.
- **SB-TB4K PSU.** Plugged into the power strip mounted on the
  back-underside of the desk.
  - **TODO:** mount the PSU brick onto the Underware 3D-printed grid
    under the desk (so it isn't just hanging off the power-strip
    cable).

## Out of scope (for now)

- Wall power, surge protection, under-desk power routing — possible
  later sweep.
- Audio routing (monitor speakers vs. headphones vs. KVM audio jack).
