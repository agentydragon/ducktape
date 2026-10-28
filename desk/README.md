# Desk wiring

Working notes for the home desk: every peripheral hangs off a Thunderbolt KVM so one button
swaps the desk between **atlas** (see <../README.md>) and a hot-plug laptop. Living doc for
inventory, topology, cable plan, routing, mounting and open questions; chronological
experiments and hypothesis updates live in <debug/build_log.md>.

**North star — achieved 2026-07-01** (Plan A, § Target topology): one-button switching between
atlas and the docked laptop, each on the SB-TB4K by a single USB-C cable, with monitor,
keyboard, camera and underdesk hub downstream of the KVM.

## Desired state

- **Two hosts on the SB-TB4K**: `atlas` (permanent) and a rotating laptop slot (currently
  `rugged`).
- **One KVM press switches all shared peripherals atomically**: AORUS FV43U (video + audio); its
  built-in USB hub (back through the monitor USB-C uplink → KVM downstream); the USB-A camera
  (FV43U lower USB-A) and TEX Shura keyboard (FV43U upper USB-A), both riding the monitor hub so
  no extra KVM USB-A run is needed; the underdesk USB-A hub on a KVM USB-A port.
- ~~**Both host ports receive PD continuously**~~ **Not achievable with the SB-TB4K**: per
  Sabrent's forum, PD to both host ports would violate TB4 certification, so only the active
  host charges (see the North-Star experiment in the build log). Workaround: plug the docked
  laptop's own AC adapter alongside the KVM cable.

### Future desiderata (not blocking Plan A)

- **Gaming on the RTX 5090s (wired 2026-07-05)**: RTX 5090 `01:00.0` DP-OUT → FV43U DP 1.4 in
  (Ivanky 8K DP m-m); keyboard and camera via FV43U USB-B uplink → atlas rear USB-A (bus 3
  port 2) → QEMU port-path passthrough to wyrm2 at `3-2.1` (keyboard) and `3-2.2` (camera).
  Monitor dual-KVM OSD binds USB-B ↔ DP and USB-C ↔ USB-C, so one press switches video and hub
  between the host/KVM path (USB-C) and wyrm2's local `seat0` (DP/USB-B). wyrm2 runs GNOME on
  that seat; per-title `gamescope -f -- %command%` gives direct scanout. DP audio via `01:00.1`
  passthrough (PipeWire → FV43U speakers). Sunshine/Moonlight available but software-encode
  only. Open: session-teardown hardening. See
  `nix/docs/wyrm2_display.md`.

## Devices on hand

- **atlas** — Proxmox host. iGPU → mobo TB4-OUT → KVM → FV43U USB-C (Proxmox console + seat0
  SPICE). Both RTX 5090s VFIO-bound to the wyrm2 VM: host `01:00.0` (ZOTAC, IOMMU group 14) →
  hostpci0 → guest `01:00.0` = display GPU (DP-OUT → FV43U DP 1.4 in, DRM connector
  `card0-DP-1`; whole device `0000:01:00` passed so audio function `01:00.1` comes along for DP
  audio); host `03:00.0` (Gigabyte, IOMMU group 16) → hostpci1 → guest `02:00.0` = headless
  compute. GPU DP-OUT → mobo DP-IN loopback removed 2026-07-01.
- **AORUS FV43U** — 43" 4K@144 monitor. Inputs: 1× DP 1.4, 2× HDMI 2.1 (24 Gb/s), 1× USB-C
  (DP-Alt + USB data + PD). USB hub: 1× USB-B uplink, 2× USB-A downstream. 2× 3.5 mm jacks
  (headphone, line-out). Internal "dual-KVM" toggles which uplink (USB-B vs. USB-C) feeds the
  hub. Hub chip: Realtek RTS5411 (`0bda:5411`).
- **Sabrent SB-TB4K** — TB4 KVM. 2× TB4 host (PC1, PC2) + 3× TB4 downstream (40 Gb/s, 60 W PD
  per port) + 4× USB-A 3.2 Gen 2 (10 Gb/s, 5 V / 2.4 A). **No standalone DP output** — video
  goes over TB4 downstream USB-C. USB `2eb9:0123` (SSI TBT4 KVM HUB).
- **TEX Shura** — 60% mech with trackpoint, USB-C jack at the back, `04d9:0532` (Holtek). One
  unit; lives permanently on the FV43U upper USB-A port, so it follows the monitor's dual-KVM
  switch: USB-C (Sabrent KVM → atlas/laptop) or USB-B (wyrm2 local `seat0`: atlas rear USB-A bus
  3 port 2 → QEMU `3-2.1` → wyrm2). No manual replug.
- **USB-A camera** — Logitech C920 HD Pro atop the FV43U, on the monitor's **lower** USB-A
  downstream port. In wyrm2 mode passed by port path `3-2.2` as QEMU `usb4` on the guest's
  high-speed EHCI bus.
- **Underdesk USB-A hub** — left-underside of the desk; USB-A uplink plugs directly into a KVM
  USB-A port.
- **USB WiFi adapter** — model TBD. Spare; possible temporary wireless NIC for atlas.
- **Atlas front-panel USB-C** — see § Front-panel USB-C passthrough.

## Cables on hand

At home now; storage-unit items are listed separately so plans don't count on them.

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

Plan A (wired): one USB-C monitor link carries DP-Alt video + hub data + PD, so the camera rides
the FV43U hub back to the KVM-switched bus. Plan B (DP video + separate USB A→B hub uplink) is a
fallback; its cables are on hand. Physical schematic (devices, ports, cables, grommets, colour
map): <wiring_schematic.svg> (source <wiring_schematic.dot>).

Free after Plan A: SB-TB4K downstream TB4 B and C, SB-TB4K USB-A 2/3/4, FV43U 2× HDMI 2.1. FV43U
DP 1.4 and USB-B uplink are used by the gaming display path.

### Port-assignment caveats

- **atlas RTX 5090 DP-OUT slot** — atlas has 2× RTX 5090. Which card
  and which DP port feeds the internal DP m-m to the mobo isn't
  recorded yet.
- **SB-TB4K downstream port choice (A vs. B vs. C)** — they're
  symmetric; any one can carry the monitor video. Pick by cable reach.
- **Which SB-TB4K USB-A number** each accessory ends up on is drawn
  arbitrarily; label the physical port once cables are pulled.

## Cable plan vs. inventory (Plan A)

One bullet per physical link: source → destination, cable, state.

- **atlas internal video**: RTX 5090 DP-OUT (slot ?) → mobo DP-IN, DP m-m. Removed 2026-07-01;
  display comes from the iGPU. Not needed for gaming (Sunshine streaming — see
  `debug/atlas/gpu_strategy.md`).
- **atlas → KVM**: mobo TB4-OUT (**middle** port) → SB-TB4K PC1, USB-C TB-class. Cabled —
  Sabrent-looking 2 ft, tag "4". Atlas's top TB port carries BIOS video but not kernel DP-Alt;
  the middle port works after kernel takeover, so the cable belongs there.
- **Laptop → KVM**: laptop TB4 (USB-C, right-drawer position) → SB-TB4K PC2. In use — 20 Gb/s
  USB 3.2 Gen 2×2 cable, plugged into rugged as laptop stand-in. Not TB4, but the SB-TB4K
  accepts it and switches cleanly.
- **KVM → monitor (combined)**: SB-TB4K downstream TB4 → FV43U USB-C in (video + hub + PD),
  Silkland 40 Gb/s 200 W. Replaces the visibly damaged 20 Gb/s cable that was flaky under
  strain.
- **Monitor hub → keyboard**: FV43U upper USB-A → TEX Shura USB-C, USB-A → USB-C. Runs monitor →
  back-right grommet → under-desk → keyboard; switches with the KVM via the monitor USB-C
  uplink.
- **KVM → underdesk hub**: SB-TB4K USB-A → hub uplink, direct. USB-A F↔M extension available if
  reach is short.
- **Monitor hub → camera**: FV43U lower USB-A → camera, direct.

All links buildable with cables on hand. Open cable questions: the tag-4 cable's TB4 marking and
the underdesk USB-A reach.

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

Direct DP output from wyrm2's display 5090 to the monitor. The monitor's dual-KVM OSD (USB-B ↔
DP, USB-C ↔ USB-C) switches video input and hub uplink in one press between the host/KVM
(USB-C) and wyrm2 local `seat0` (DP/USB-B) paths.

- **wyrm2 display → monitor**: RTX 5090 `01:00.0` DP-OUT → FV43U DP 1.4 in, Ivanky 8K DP m-m.
- **Monitor hub → wyrm2 input**: FV43U USB-B uplink → atlas rear USB-A bus 3 port 2 → QEMU →
  wyrm2, USB A → B. Keyboard at QEMU path `3-2.1`, C920 at `3-2.2` (EHCI `usb4`) when the
  monitor is in USB-B mode.

**Gotcha: the USB A→B cable must stay in the atlas port the passthrough is pinned to.** wyrm2's
`usb3:`/`usb4:` pin **physical host port paths** (`host=3-2.1`, `host=3-2.2`), not VID/PIDs. Move
the cable and the KVM press still switches video to wyrm2, but the keyboard silently lands on
the atlas host (`usbhid` binds it; QEMU watches the vacated port). Pinning by the Shura's
`04d9:0532` instead would make QEMU seize the keyboard on the USB-C path too, stealing it from
atlas in work mode. **Re-pin whenever the cable moves**:

```bash
# 1. With the monitor switched to USB-B, find where the hub landed:
ssh root@atlas 'dmesg -T | grep -E "idProduct=(0532|5411)" | tail -4'
# 2. Re-pin (config for next boot):
ssh root@atlas 'qm set 110 -usb3 host=<bus>-<port>.1,usb3=1'
# 3. Apply to the *running* VM — `qm set` only stages it (see below):
ssh root@atlas 'qm monitor 110 <<< "device_del usb3"'
ssh root@atlas 'qm monitor 110 <<< "device_add usb-host,bus=xhci.0,hostbus=<bus>,hostport=<port>.1,id=usb3"'
```

**Deviation from stock Proxmox:** despite `hotplug: network,disk,cpu,usb`, `qm set -usbN` stays in
`qm pending` and never reaches the running VM (verified 2026-09-04, including delete-then-re-add);
the QEMU-monitor `device_del`/`device_add` pair is the live path.

**Finding the port without switching the monitor:** the hub's SuperSpeed half (`0bda:0411`)
stays enumerated on atlas in USB-C mode (`lsusb -t`, bus 4), and
`/sys/bus/usb/devices/usb4/4-0:1.0/usb4-portN/peer` maps SS port N to the HS port the keyboard
will use.

### Front-panel USB-C passthrough

Atlas's front-panel USB-C port is pinned by host path `3-1` and passed to wyrm2 VM 110 as QEMU
`usb1`, so any device plugged there reaches wyrm2 (validated with YubiKey `1050:0407`, which is
not the passthrough's identity).

## Cable marking

Deferred until the marker paper leaves storage. Options: coloured bands at each end, colour =
port group (host TB / video / USB-downstream); or numbered tags `01..NN` keyed here.

## Cable routing

- **KVM → monitor (Silkland USB-C)**: through the **back-right grommet**; KVM end under the desk.
- **Monitor → keyboard (USB-A → USB-C)**: FV43U upper USB-A → back-right grommet → under the
  desk → up to the TEX Shura in the keyboard slot.
- **KVM → laptop slot (20 Gb/s USB-C)**: under the desk, up through the **right-drawer
  grommet**. Worn end deliberately at the laptop (visible, easy to replug), clean end at the KVM.
- **5090 → monitor DP** and **monitor USB-B → atlas USB-A**: not yet recorded.

## Open questions

- Is the "tag 4" ~2 ft Sabrent-looking cable (now the atlas host link) actually TB4-marked?
- Which SB-TB4K host port (PC1 vs. PC2) each host cable is on.
- KVM placement (desktop vs. underdesk) — affects all USB-A cable lengths.
- Per-link cable-length constraints; measure as we go.
- Does the FV43U dual-KVM binding survive monitor power cycles?
- Routing of the gaming-path cables.

## Networking (separate from KVM plan)

If atlas's in-use Ethernet path fails: the on-hand shielded Ethernet cable if it reaches (short),
or the USB WiFi adapter as a stopgap NIC.

## Placement & mounting

- **SB-TB4K**: temporarily on top of the atlas tower. TODO: mount to the desk or under-desk grid
  (magnetic tape on a ferromagnetic surface, else Velcro / 3M-Command adhesive).
- **KVM toggle (wired remote button)**: beside the keyboard for thumb reach. TODO: mount it
  (ferromagnetic body; magnetic tape is the lead candidate).
- **SB-TB4K PSU**: on the power strip at the desk's back-underside. TODO: mount the brick on the
  Underware 3D-printed under-desk grid.

## Out of scope (for now)

Wall power, surge protection, under-desk power routing; audio routing (monitor speakers vs.
headphones vs. KVM audio jack).
