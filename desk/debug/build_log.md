# Desk build log

Chronological log of experiments, physical changes, and hypothesis
updates during the desk build. Current state — devices, cables,
target topology, cable plan, mounting TODOs, cable routing — lives
in <../README.md>; this file is history.

## 2026-06-30 — Plan A bench test with rugged as host

**Setup.** First end-to-end test of the Plan A monitor link, using
rugged (Dell Rugged 12 tablet, per `nix/nixos/hosts/rugged/`) as a
stand-in for the laptop slot. Wiring:

- rugged TB4 (USB-C) → short TB cable → SB-TB4K host port (PC1 or PC2;
  not recorded which).
- SB-TB4K downstream TB4 → 20 Gb/s 8K USB-C cable → FV43U USB-C input.
- TEX Shura plugged directly into a rugged USB-A port (KVM bypass for
  this test).
- Pixel 6 plugged into one of the FV43U USB-A downstream ports via a
  USB-A → USB-C cable, to exercise the monitor's hub through the same
  USB-C uplink.

**Observations.**

- Video to the FV43U: works.
- Audio out via the monitor: works. Adjusting the monitor's volume
  control changes the output volume — i.e. the host is targeting the
  monitor as the sink.
- FV43U USB hub (over USB-C uplink): works — Pixel 6 is recognized.
- **Stability: flaky.** Link dropped a couple of times during the
  test. Working hypothesis: the 20 Gb/s 8K USB-C cable between the
  SB-TB4K and the monitor is currently bent, and the bend is enough to
  marginalize the connector or shielding.

**Conclusion.** Plan A's combined DP-Alt + USB-3.2 stream is
electrically feasible through this stack of cables — but not yet
proven reliable. Next moves:

- Reroute / straighten the 20 Gb/s cable and re-test before changing
  anything else.
- If still flaky, swap to Plan B (Ivanky USB-C ↔ DP for video, USB
  A→B for the hub uplink) and re-bench.
- Independently: get the TEX Shura on a KVM USB-A port so we're
  testing the real target topology.

## 2026-06-30 — TEX Shura on KVM USB-A

**Setup.** Moved the TEX Shura off rugged's direct USB-A and onto a
SB-TB4K downstream USB-A port (specific port number not recorded).
USB-A → USB-C cable, same Plan A monitor link as the previous test.

**Observation.** Keyboard works through the KVM.

**Conclusion.** "KVM USB-A → keyboard" row of the cable plan is
confirmed for the rugged-host case. With the keyboard now off the
host directly, all of (video, audio, monitor hub, keyboard) are on
the KVM-shared bus — the only target-topology link still bypassed is
the camera (model TBD) and the underdesk hub (uplink TBD).

## 2026-07-01 — rugged on the laptop-side slot; Plan A end-to-end

**Setup.** Rugged unplugged from its earlier direct-to-KVM short TB
cable and plugged into the **laptop end** of the Silkland cable
(right-drawer position, cable routed through the drawer grommet, KVM
end already landed on a SB-TB4K host port). All other Plan A links
per the target topology.

**Observations.**

- Rugged switched its video output to the FV43U.
- Audio out via the monitor still works.
- TEX Shura on a KVM USB-A port continues to work with rugged as the
  active host.
- Rugged is **charging over the TB link** — Power Delivery is passing
  through the KVM host port and the Silkland cable.

**Conclusion.** Validates the Silkland end-to-end (through the
grommet + into a KVM host port + into rugged) and validates the whole
KVM-shared-bus chain from the intended laptop position, not just from
the earlier short-cable test rig. Also validates PD upstream through
the KVM host port. Still to validate: the **atlas-side** switch —
toggle the KVM to atlas's host port and confirm atlas sees the FV43U
as its output.

## 2026-07-01 — atlas first boot on the KVM chain: BIOS/GRUB visible, then "USB-C no signal"

**Setup.** atlas powered on for the first time with Plan A wiring, KVM
toggled to the atlas-side host port. No network to atlas: USB WiFi
stick not installed yet, no ethernet reachable, current apartment's
WiFi SSID not configured on atlas.

**Observations.**

- BIOS splash: visible on the FV43U.
- GRUB menu: visible.
- A few lines of early kernel syslog scroll: visible.
- Then the monitor drops to "USB-C no signal" and stays there.

**Interpretation (working hypothesis).** BIOS / GRUB / early kernel
paint on one RTX 5090's firmware framebuffer, which does reach the
monitor via the mobo DP-IN → TB-OUT → KVM → 20 Gb/s USB-C → FV43U
USB-C. Once the Linux kernel finishes loading and `vfio-pci` binds
both RTX 5090s for wyrm2 passthrough, the framebuffer handoff drops
atlas's console output — that's the "no signal" moment. If wyrm2
autostarts and its guest driver outputs on the same physical DP port,
video should come back; in this test it didn't (within the time we
watched), so wyrm2 either isn't autostarting or is outputting to a
different DP.

**Conclusion.** The whole desk-side wiring — mobo TB-OUT → Sabrent
tag "4" → KVM PC port → 20 Gb/s USB-C → FV43U USB-C — is confirmed
end-to-end for atlas up through kernel boot. The remaining blocker is
atlas-side software: no display source for the Proxmox host after
`vfio-pci` binds the GPUs, and no way to diagnose because atlas has no
network yet.

**Additional test — KVM bypass.** Reconnected the FV43U's USB-C
directly into the same atlas TB port that had been feeding the KVM
(the **top** TB port on atlas). Result: monitor shows a **gray
screen** — link is up (EDID negotiated) but no useful content. Same
phenomenon as with the KVM in-line, so the KVM chain is out of the
diagnosis. User reports the same symptom on this hardware at the
previous apartment.

**Additional test — different atlas TB port.** Moved the direct-
connect FV43U USB-C from atlas's **top** TB port to atlas's **middle**
TB port. This works: **atlas is signed in with the desktop console
live on the FV43U** and the keyboard usable via the monitor's built-in
USB-A hub. So the earlier "gray screen" wasn't a `vfio-pci` /
framebuffer-handoff issue after all — it was port-selection. The
mobo's TB DP-Alt output is only wired to the middle TB port after the
kernel initialises, not the top one (BIOS paints all ports; kernel
paints only middle).

**Current physical state.** For local atlas setup:

- FV43U USB-C is on atlas's **middle** TB port directly (KVM still
  bypassed for atlas video).
- TEX Shura is on the FV43U's built-in USB-A hub, giving atlas the
  keyboard.

**Follow-ups.**

- Get network to atlas — cheapest path is the on-hand USB WiFi stick;
  fallback is a rescue USB to drop new WiFi creds into
  `wpa_supplicant.conf` or an SSH key into `authorized_keys`.
- Once SSH-able: check `virsh list --all` / `qm list` for wyrm2's
  autostart state; check which GPU (bus:slot.function) is passed
  through and which physical DP port that GPU's guest driver is
  outputting to.
- Since the KVM-in-line and direct-connect tests both produce the
  same gray-screen state, the KVM stays out of scope for this
  diagnosis. Debugging is atlas config, not desk wiring.
- Longer-term: understand _why_ only the middle TB port carries kernel
  DP-Alt (BIOS setting? mobo silkscreen? IOMMU group of the top port
  is passed through?). Could also give atlas a display source that
  survives full VFIO passthrough (iGPU or spare GPU outside the
  passthrough set) so console debugging isn't dependent on TB port
  choice.

## 2026-07-01 — regression: direct middle port stops working; 20 Gb/s cable visibly damaged

**Setup.** Same direct-connect wiring as the earlier middle-port
success: FV43U USB-C ↔ 20 Gb/s USB-C ↔ atlas middle TB port. No KVM.

**Observation.** Monitor now shows "no USB-C signal" from that path,
even though the identical wiring worked earlier in the session. The
20 Gb/s USB-C cable is **visibly wonky on one end** — likely
mechanical damage at the connector.

**Reframing.** This calls the earlier "middle port works, top port
doesn't" experiment into question. If the 20 Gb/s cable is
intermittent, some of that session's signal / no-signal outcomes may
have been aliased by cable behaviour rather than by which TB port on
atlas we were trying. The "atlas via KVM (middle port) → no signal"
result from the KVM-inline retry is also probably contaminated: the
20 Gb/s cable is on the KVM → monitor leg there.

**Follow-ups.**

- Swap the 20 Gb/s cable out for a known-good USB-C. Cheapest test:
  temporarily grab the Silkland from the laptop-side Silkland run
  (rugged isn't being used) and put it on atlas ↔ FV43U USB-C.
- Or: try the Ivanky USB-C ↔ DP cable into the FV43U's DP1 input for
  a fully independent path.
- Once cable-quality noise is removed, re-run both port variants
  (top and middle) direct-connect, then rebuild the KVM chain and
  redo the atlas-via-KVM test. That's the point at which any port
  distinction should be believable.
- Retire the 20 Gb/s cable from load-bearing links; it may still be
  usable as a low-stakes spare, but not for anything that has to be
  stable.

## 2026-07-01 — KVM back in the chain, atlas on middle port

**Setup.** Sabrent tag "4" cable moved to atlas's **middle** TB port
on one end, SB-TB4K host port on the other. Rugged still on the
Silkland at the drawer position. KVM downstream → 20 Gb/s USB-C →
FV43U USB-C unchanged.

**Observations.**

- Rugged → KVM → FV43U: works.
- Atlas → KVM → FV43U (middle port via Sabrent tag "4"): "no USB-C
  signal".

**Interpretation.** Direct-connect from atlas's middle port to the
FV43U via the 20 Gb/s USB-C cable worked. Add the KVM + swap the
inline cable to Sabrent tag "4" and it doesn't. Two likely
candidates:

- The Sabrent tag "4" cable can't carry DP-Alt at whatever rate atlas
  is trying to negotiate from the middle port (even though it did
  carry BIOS-era video from the _top_ port earlier — top port likely
  negotiated a lower link rate). This is the leading suspicion; the
  tag "4" TB4 marking has never been visually verified.
- The KVM host port that atlas is on isn't tolerating atlas's TB
  handshake. Less likely given rugged negotiates fine on the other
  host port.

**Follow-ups.**

- **Swap-cable test:** put the 20 Gb/s USB-C cable on atlas → KVM
  (middle port) and put the Sabrent tag "4" cable on KVM downstream →
  FV43U. If atlas video reaches the FV43U through the KVM in that
  configuration, the tag "4" cable is at fault at the source-side
  link rate.
- Prioritize this after atlas is SSH-able; user is pausing this
  thread to get atlas on the network and merge the current doc as a
  base for further troubleshooting.

## 2026-07-01 — Silkland cable direct-connect, both atlas ports; same "no USB-C signal"

**Setup.** Freed the Silkland cable from the rugged/KVM path and put
it directly between atlas's TB port and the FV43U's USB-C input.
Tried atlas's middle TB port first, then the top port. Atlas was not
restarted between any of the cable / port changes in this session.

**Observations.**

- Monitor detects a connection each time (link-layer up — user notes
  "obviously there's some kind of electricity going around").
- Monitor reports "USB-C no signal" on both ports with the Silkland
  cable, i.e. no video packets are being sent.

**Reframing.** This exonerates the cable — the Silkland is confirmed
TB-class and worked end-to-end with rugged earlier. And it's not just
the top port that's broken, both ports behave the same. So the
"middle works, top doesn't" finding really was aliased by the 20 Gb/s
cable's flakiness; on this pass with a good cable, neither port
paints video.

**Leading hypothesis.** Atlas's TB port(s) are stuck in a bad DP-Alt
state after the earlier KVM + hot-plug shuffles. TB controllers can
fail to renegotiate DP-Alt on the next connect once they've been
hot-unplugged via a switch — the kernel driver still "owns" the
port but nothing is drawing to it. No atlas reboot between the port
swaps means whatever wedged, stayed wedged.

**Follow-ups (before waiting on SSH).**

- Cable **source-side hotplug**: unplug at the atlas side (not just
  the monitor), wait 10 s, replug. Some TB controllers only
  renegotiate DP-Alt on source-side re-insertion.
- If that doesn't clear it, **reboot atlas**. BIOS/GRUB will paint
  on whatever port carries firmware framebuffer (previously the top
  port), which unblocks direct console access without needing the
  network.

**Follow-ups (after SSH).**

- `dmesg -T | grep -Ei 'thunderbolt|drm|nouveau|nvidia|vfio' | tail -100`
  to see the TB / display-driver state around the last connect
  attempt.
- Check `/sys/bus/thunderbolt/devices/` and
  `/sys/class/drm/*/status` to see whether the kernel currently
  believes anything is plugged into the port.

**In parallel.** User is still hunting for the USB WiFi stick; plan
B once video is back is to tether atlas to a phone hotspot.

## 2026-07-01 — isolate: keyboard-into-atlas + DP off a non-loopback GPU port

Two isolation tests to rule out simpler explanations.

**Test A — keyboard directly into atlas.** Moved TEX Shura off the
FV43U hub and into a rear USB-A port on atlas itself, with the FV43U
USB-C into atlas's top TB port. Mashing keys did not wake anything up
on the monitor: same "USB-C no signal". So the gray/no-signal isn't
"display server idle-blanked, needs input" — atlas is not painting
video regardless of input.

**Test B — DP off a different GPU port.** Cabled DP from a second
DP-OUT on one of the RTX 5090s (a port that is _not_ the one feeding
the mobo's internal DP-IN loopback) into the FV43U's DP 1.4 input.
No signal there either.

**Interpretation.** Combined with the earlier findings:

- BIOS/GRUB/early kernel _did_ light up the monitor (via the mobo TB
  loopback). So one GPU + its DP-OUT does produce a valid signal
  during firmware framebuffer.
- Once the kernel finishes booting: nothing on the mobo TB output at
  any port, nothing on a direct DP-OUT off the same GPU family, and
  no wake-on-USB input either.

This is strongly consistent with the original `vfio-pci` hypothesis
that we prematurely dismissed: both RTX 5090s get bound to `vfio-pci`
for wyrm2 passthrough at kernel boot, all their outputs go dark
because atlas Proxmox itself has no console GPU left, and — because
wyrm2 either doesn't autostart or is configured to output on some
port we haven't tried yet — nothing else drives the display either.
The earlier "middle vs. top TB port" and cable-quality distinctions
look like noise on top of that root cause.

**Follow-ups.**

- Once atlas is on the network (USB WiFi stick or phone tether):
  - `qm list --full` (or `virsh list --all`) — is wyrm2 running?
    `qm config <wyrm2 vmid>` — is `onboot: 1`? Which DP output does
    its guest driver target?
  - `lspci -nnk | grep -A3 -Ei 'nvidia|thunderbolt'` — driver
    binding on the RTX 5090s post-boot.
  - `dmesg -T | grep -Ei 'vfio|thunderbolt|drm|nouveau|nvidia'` —
    the exact handoff moment.
- Before SSH: try DP from _each_ DP-OUT on _each_ RTX 5090 to the
  FV43U DP input in turn. If wyrm2 _is_ up but outputting to a
  specific port, one of the eight-or-so ports will show something.
- Ultimate fallback: rescue USB, mount `/`, either
  disable-vfio-in-GRUB for one boot, or fix `wyrm2` autostart config
  from the recovered filesystem.

## 2026-07-01 — planned test: pull the DP loopback, DP straight from GPU to monitor, cold-boot atlas

Immediate objective narrowed to "get atlas connected to monitor +
keyboard". Everything KVM- / TB- / hub-shaped is now variable to
eliminate.

**Planned setup.**

- Remove the internal DP m-m between the RTX 5090 DP-OUT and the
  mobo's DP-IN (i.e. dismantle the TB loopback source).
- Cable that same GPU DP-OUT directly to the FV43U's DP 1.4 input.
- TEX Shura stays on a rear USB-A port on atlas itself.
- Reboot atlas from cold.

**Expected outcomes.**

- BIOS / GRUB / early kernel should paint on the FV43U via straight
  DP (same framebuffer path that was reaching us over the TB
  loopback earlier). If _that_ doesn't happen, something else is
  wrong with the GPU or its DP-OUT.
- Handoff moment: if the display stays lit past kernel init, then
  either `vfio-pci` isn't binding that GPU or wyrm2 is up and
  driving it via passthrough. Either way, we're in.
- If the display dies at handoff, we've confirmed the VFIO cliff on
  the shortest possible cable path — no more variables to blame —
  and the next step becomes "boot without VFIO for one cycle to fix
  the wyrm2 config".

## 2026-07-01 — direct DP boot: greeter, violent reboot, EFI-stub-then-artifact stall

**Observations.**

- First boot with GPU → DP → FV43U direct produced BIOS, GRUB, the
  full kernel boot log, and eventually **a greeter**. So atlas can
  paint the monitor via this GPU's DP-OUT all the way to a login
  prompt — which strongly suggests wyrm2 autostarted and drove the
  DP through VFIO passthrough (the Proxmox host itself has no
  console GPU, so the greeter has to be coming from the guest).
- Then atlas **rebooted "violently"** — could be an unattended
  update install, a wyrm2 guest kernel panic taking the host video
  with it, or a hardware reset. Not yet clear which.
- Second boot got as far as `EFI stub: Loaded initrd from
LINUX_EFI_INITRD_MEDIA_GUID device path`, then the framebuffer
  froze with faint horizontal purple/green artifact lines and
  stayed there for at least a minute. That's the firmware
  framebuffer going stale as the kernel takes over, with no repaint
  yet from either the kernel or the guest.

**Interpretation.** VFIO cliff confirmed on the direct-DP path;
wyrm2's guest is the only thing painting video on this box, and it
takes a while to come up after Proxmox boots. The greeter on boot 1
is the important positive datapoint — it means the whole video path
works when the guest is running. The stall on boot 2 is expected
until wyrm2 boots enough to reclaim the DP-OUT.

**Follow-ups.**

- **Wait 2–3 minutes** on any given boot before concluding it's dead.
- If the display stays stuck past that: **cable-hotplug the DP** at
  either end to force a re-negotiation once the guest is up.
- Once a greeter appears again, **first** action is to open a
  terminal, drop an SSH authorized_key, ensure the SSH service is
  running, and grab the IP — before any other work, so the next
  reboot doesn't strand us.
- Look into what caused the "violent reboot" via `journalctl -b -1`
  once SSH is up.

## 2026-07-01 — waited, greeter appeared at correct resolution

**Observation.** Left atlas alone on the "EFI stub" stalled screen;
after a couple more minutes the full-size greeter appeared at the
correct resolution — same GPU DP-OUT → FV43U DP path.

**Conclusion.** VFIO cliff + slow wyrm2 autostart confirmed: the
display is dead for the couple minutes between kernel handoff and
guest driver claiming the DP, then normal. So the desk video path
works — the bring-up delay is atlas config, not wiring.

**Immediate next step (top priority).** Get SSH set up **while the
greeter is live** — before the next reboot strands us. Log in →
`sudo systemctl status ssh` → drop pubkey into `authorized_keys`
(`curl https://github.com/agentydragon.keys >> ~/.ssh/authorized_keys`
is one path) → `ip -brief addr` for the IP → test SSH from another
device on the same network. Only after that, dig into
`journalctl -b -1` for the violent-reboot cause and check wyrm2
autostart config.

## 2026-07-01 — correction + new attempt: no loopback, monitor on top TB, keyboard in back USB-A

**Corrections to earlier hypotheses.**

- The greeter I was calling "wyrm2's guest" was atlas's own Proxmox
  desktop greeter. So atlas keeps a GPU available for its own console
  — the sweeping "VFIO killed all video" story was wrong. Every
  "signal comes then goes" observation earlier was a symptom of some
  other layer (cable strain / port renegotiation / DPMS blanking),
  not the VFIO cliff I kept invoking.
- The "keyboard doesn't work in atlas" was tested against known-good
  hardware: swapped in a Das Keyboard (verified working on rugged with
  the same cable), same silence. So the earlier no-input state was
  not Shura-specific.

**New wiring for this attempt.**

- Removed the internal GPU DP-OUT → mobo DP-IN loopback.
- Monitor USB-C → atlas's **top** TB port (was previously the
  "doesn't work after kernel" port; retrying without the loopback in
  play).
- Das Keyboard in a rear USB-A port on atlas.

**Progress so far.**

- BIOS logo visible.
- GRUB visible; **Enter registers from the keyboard** — first
  concrete positive on input reaching the box at any stage.
- Kernel boots for a while, then does a mode-switch to a full-size
  resolution — DRM driver is actively painting through the top TB
  port, not just firmware framebuffer.
- Currently: `Atlas booting…` messages, waiting for greeter.

**Immediate next step (as soon as greeter appears).**

- Confirm keyboard input in the login box.
- Log in.
- Set up SSH: `sudo systemctl enable --now ssh`;
  `curl https://github.com/agentydragon.keys >> ~/.ssh/authorized_keys`;
  `ip -brief addr` for the IP; SSH-test from another device before
  touching anything else. This finally breaks the "can't fix atlas
  because I can't get into atlas" loop.

## 2026-07-01 — atlas SSH-able via wyrm2 as a jump host

**Path taken.** Wifi stick was USB-passed-through to wyrm2, so wyrm2
came online but atlas host stayed offline. Both `vmbr0` on atlas and
`ens18` on wyrm2 were UP but had no IPv4. Assigned matching static
addresses (`192.168.100.1/24` on atlas `vmbr0`, `192.168.100.2/24` on
wyrm2 `ens18`), got ping, then SSH-as-root from wyrm2 to atlas
(user's key already authorised).

**Consequence.** The dependency loop that has blocked atlas debugging
all day (need input to fix atlas, need atlas working to have input)
is now broken. All further atlas work can happen over SSH from
wyrm2 (or via wyrm2 as a jump host from any other machine wyrm2 can
be reached from — LAN, Nebula mesh, etc.).

**Follow-ups.**

- Persist the atlas `vmbr0` IP so this doesn't vanish on the next
  reboot (edit `/etc/network/interfaces` static stanza — see also
  what the Proxmox web UI was expecting on that bridge normally).
- Sort atlas's own outbound networking so it doesn't need wyrm2 to be
  up (or accept that wyrm2 is the jump-host of record for the desk).
- Now, at leisure: `journalctl -b -1` for the violent reboot cause,
  wyrm2 autostart config, why input was silent in the earlier
  wiring, and return to Plan A KVM wiring for the desk itself.

## 2026-07-01 — KVM back in the chain; video + USB both work

**Setup.** With atlas back up after the accidental power-button
press and reboot: put the KVM back inline.

- atlas top TB port → Sabrent tag "4" (short) → SB-TB4K host port
  (PC1 or PC2 — record which one on the next look).
- SB-TB4K downstream TB4 → **Silkland** → FV43U USB-C.
- TEX Shura on the FV43U's built-in USB-A hub (mouse also on it).
- The visibly-damaged 20 Gb/s USB-C cable is retired to spares.

**Observations.**

- Video from atlas to the FV43U works.
- USB works: mouse events reach atlas via monitor-hub → USB-C uplink
  → KVM downstream → KVM host → atlas.

**Conclusion.** Plan A is functionally live on the atlas side. Only
the laptop-side host link is missing (needs another TB-class USB-C
cable; the right-drawer grommet is reserved for it).

## 2026-07-01 — North Star: both hosts on the KVM, one-button switching works

**Setup.** Filled the laptop-side slot with the previously-retired
20 Gb/s USB 3.2 Gen 2×2 USB-C cable, from SB-TB4K host PC2 to
rugged. Cable was flagged as visibly damaged and had been unreliable
on the KVM → monitor leg; put it here as a "does it work at all"
test.

**Observations.**

- With KVM toggled to laptop: rugged's video reaches the FV43U;
  mouse events reach rugged.
- Press the KVM toggle: atlas becomes the active host; its video
  reaches the FV43U; mouse events reach atlas.
- The 20 Gb/s cable works fine in this role, contrary to its earlier
  behaviour on the KVM → monitor leg. Either the KVM handshake is
  more forgiving than the FV43U's USB-C input, or the earlier
  flakiness was really about how the cable was bent, not the cable
  itself.

**Conclusion.** North Star achieved. The full Plan A topology is
live: two hosts on the KVM, monitor + hub + keyboard + camera
downstream, single button switches everything.

**Known gap.** When rugged is _not_ the active host on the KVM, it
does not charge. The desired-state spec calls for both host ports to
receive PD continuously so a docked laptop always charges — this
requirement isn't met yet. Not critical (user is fine with it for
now), but recorded as a known deviation.

**Diagnosis (confirmed).** Rugged _does_ charge while it's the
active host, but stops charging when the KVM is toggled to atlas.
That rules out the cable and localises the problem to the KVM's
default PD-routing behaviour: only the active host gets PD.

**Root cause (confirmed via Sabrent).** The SB-TB4K only delivers
PD to the currently-active host by design. Per Sabrent's community
forum, dual-host simultaneous charging would violate Thunderbolt 4
certification, so it isn't a firmware/mode limitation — it's the
hardware.

- <https://sabrent.com/community/xenforum/topic/152900/thunderbolt-4-kvm-charging>
- <https://sabrent.com/community/xenforum/topic/128405/sb-tb4k-pd-power>

**Resolution.** Retired the "both hosts get continuous PD" clause
from the desired-state spec — it's unachievable with this KVM.
Workaround for the docked laptop: plug its own AC adapter alongside
the KVM cable when it needs to charge while atlas is active. If
this ever needs to be a hard requirement, we'd need a different
switch (probably a non-TB-certified USB-C dock).

## 2026-07-02 — remote recheck from wyrm2; gaming display path decided

**Remote verification** (SSH from a wyrm2 session, no physical changes):

- Sabrent KVM authorized on atlas as USB4 peripheral, 40 Gb/s both
  directions (`boltctl`).
- Full downstream chain enumerates on atlas (`lsusb`): SSI TBT4 KVM
  HUB, monitor hub (RTS5411), keyboard (Holtek), camera — identified
  as a **Logitech C920 HD Pro** (was "model TBD").
- Active display: amdgpu iGPU `DP-1`, **3840×2160 @ 60 Hz** (DRM
  state). Not yet diagnosed whether 60 Hz is a GNOME default or a
  link cap (FV43U USB-C shares lanes with USB data; 2-lane DP-Alt
  tops out ~4K60 without DSC). No action — the gaming plan targets
  60 fps.
- 5090s confirmed vfio-bound, no host output; consistent with the
  2026-07-01 loopback removal.

**Decision.** Games will render on a 5090 in wyrm2 and stream via
Sunshine → Moonlight (on atlas) over the existing iGPU → TB4 → KVM
display path at 4K60. No desk rewiring needed; the direct-DP /
monitor-dual-KVM option is shelved unless >60 Hz or VRR is ever
wanted. Plan + post-reboot checklist: `debug/atlas/gpu-strategy.md`.

**Note:** The Sunshine decision was superseded the same evening — see
the next entry.

## 2026-07-02 — gaming-display cables wired; DP locked but black

**Physical wiring (same evening).** Ivanky 8K DP m-m: RTX 5090
(`02:00.0`) DP-OUT → FV43U DP 1.4 in. USB A→B: FV43U USB-B uplink →
atlas rear USB-A. FV43U KVM Wizard bound: USB-B ↔ DP, USB-C ↔ USB-C.

**Observations.** Monitor locked 4K signal on DP input but displayed
all-black (mutter cross-GPU copy failure — virtio-primary +
nvidia-secondary compositor path). FV43U hub did not switch to USB-B
(gated on target input having signal). Full session log:
`debug/atlas/direct_display_bringup/README.md`.

## 2026-07-05 — DP cable moved to 01:00.0; gaming path confirmed

**Physical change.** Replugged Ivanky 8K DP m-m from `02:00.0` 5090
to `01:00.0` 5090 (no other cable changes). Makes render==display GPU,
eliminating cross-PCIe frame copies.

**Observations.** Sway on seat-game, per-title gamescope, DP audio
(via `01:00.1` passthrough), and Stellaris (Proton): all confirmed
working. See `debug/atlas/direct_display_bringup/README.md`.

## 2026-09-04 — keyboard lost on KVM switch: passthrough pinned to a vacated atlas port

**Symptom.** Monitor showed atlas and typed into atlas. Pressing the KVM
button switched _video_ to wyrm2 as expected, but keystrokes reached
neither host — wyrm2 saw no input at all.

**Diagnosis.** wyrm2's `usb3:` passthrough pins a physical host port path,
`host=3-12.1`. atlas's kernel log showed the FV43U hub enumerating at
`usb 3-12` (keyboard `3-12.1`) only until `03:13:10`; from `03:21` onward
it came up at `usb 3-5` / `3-5.1`. The USB A→B cable had been knocked into
a different atlas USB-A port. QEMU kept watching the vacated `3-12.1`, so
on each KVM press the keyboard enumerated on the **atlas host**, `usbhid`
bound it there, and the guest was handed nothing. Video was unaffected
because it rides the independent DP path.

Confirming detail: `usb3-port12` has **no SuperSpeed peer** (`peer` →
`peer`), i.e. the original port was USB2-only; the new one (`3-5`, later
`3-2`) pairs with a bus-4 SS port.

**Fix.** Re-pinned to the live port. Verified end-to-end at `3-5.1`:
KVM press → video _and_ keyboard + trackpoint both landed on wyrm2.
Cable then deliberately relocated to a rear port and re-pinned to
`3-2.1`.

**Deviation found (Proxmox).** With `hotplug: network,disk,cpu,usb` set,
`qm set 110 -usb3 …` still only stages the change — `qm pending` kept
showing `cur usb3: host=3-12.1` and QEMU's `info qtree` kept
`hostport = "12.1"`, including after a delete-then-re-add via `qm set`.
Applying it live needs the QEMU monitor directly:

```bash
qm monitor 110 <<< "device_del usb3"
qm monitor 110 <<< "device_add usb-host,bus=xhci.0,hostbus=3,hostport=2.1,id=usb3"
```

**Locating the port without switching the monitor.** The hub's SuperSpeed
half (`0bda:0411`) stays enumerated on atlas even in USB-C mode, so
`lsusb -t` shows which bus-4 port the cable is in; the kernel's
`usb4-portN/peer` symlinks pair SS↔HS ports 1:1, giving the bus-3 port the
keyboard will use.

**Durable consequence.** The port-path pin is load-bearing and must be
re-pinned whenever the cable moves — recorded as a gotcha in
<../README.md> § Gaming display path, with the recovery recipe.
