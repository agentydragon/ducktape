# wyrm2: missing monitor audio output

2026-10-27. Current playback recovered. Temporary contention reproduces the
missing-output failure; the original device holder is unproven.

## Failure and evidence

YouTube video played in Chrome without sound from the AORUS FV43U monitor.
Selecting the monitor in GNOME's output chooser reverted to “Speakers.”

At 15:07:26 PDT, the user's PipeWire failed to open the display GPU's audio
device. WirePlumber then failed to create its output node:

```text
pipewire[520424]: spa.alsa: 'hdmi:1': playback open failed: Device or resource busy
pipewire[520424]: mod.adapter: ... can't get format: Device or resource busy
wireplumber[520425]: s-monitors: Failed to create alsa_output.pci-0000_01_00.1.hdmi-stereo: Object activation aborted: PipeWire proxy destroyed
```

Before recovery, the card's selected profile was `output:hdmi-stereo`, its
DisplayPort route was available and unmuted, and the configured default was
`alsa_output.pci-0000_01_00.1.hdmi-stereo`. That output node was absent.
The only output node was the QEMU virtual analog device at PCI `08:0c.0`.
While YouTube played, `wpctl status`, `pw-link -l`, and `pw-dump` showed Chrome's
two audio channels connected to that virtual device. Chrome was unmuted.

The hardware still detected the monitor: `/proc/asound/card1/eld#0.0` reported
`monitor_present=1`, `eld_valid=1`, `monitor_name=AORUS FV43U`, DisplayPort,
and stereo LPCM support. At the initial inspection, all playback PCMs were
closed; no process was still holding the display audio PCM device.

The installed WirePlumber 0.5.14 script
`share/wireplumber/scripts/monitors/alsa.lua`, lines 358–367, logs a failed
node activation without scheduling a retry in that callback. The observed
failed output remained absent until its profile was recreated.

## Recovery and verification

With the display GPU's WirePlumber device ID verified as 43:

```sh
wpctl set-profile 43 0
wpctl set-profile 43 1
```

This recreated the monitor output as node 87. It became the default
automatically, and Chrome's existing stream moved to its HDMI playback ports.
`/proc/asound/card1/pcm3p/sub0/status` then reported `RUNNING`, owned by
PipeWire PID 520424. The user confirmed sound from the monitor. Node IDs are
ephemeral; inspect `wpctl status` before repeating these commands.

## Original contention: remaining uncertainty

The GDM greeter had its own PipeWire PID 3339 and WirePlumber PID 3344 until
15:07:44, overlapping the user's failed activation by about 18 seconds.
Greeter/user audio contention is a plausible trigger, but the journal does
not identify the process holding the PCM device at 15:07:26. No historical
file-descriptor capture exists to prove that owner.

The user's audio services had already been running since 08:47, before the
graphical login. The account also belongs to the `audio` group. These facts
are relevant to investigating cross-session device access, but do not prove
the original holder. The SSH session began at 08:45:29; the graphical session
was not created until 15:07:38, about 11 seconds after the failure. Thus the
user's failed activation occurred before their graphical login, while the
greeter still owned the active seat. Both WirePlumber instances use separate
session D-Bus connections for `ReserveDevice1`, so those reservations do not
coordinate device access across the two users.

## Controlled reproduction

At 17:24, an isolated PipeWire daemon held the unused QEMU playback device
`hw:3,0` open. The monitor output continued playing Chrome throughout.
Cycling the QEMU card's profile Off and back to its original Analog Stereo
Duplex profile reproduced the original error chain:

```text
pipewire[520424]: spa.alsa: 'front:3': playback open failed: Device or resource busy
pipewire[520424]: mod.adapter: ... can't get format: Device or resource busy
wireplumber[520425]: s-monitors: Failed to create alsa_output.pci-0000_08_0c.0.analog-stereo: Object activation aborted: PipeWire proxy destroyed
```

After the isolated daemon released the PCM device, its ALSA status was
`closed`, but the main session's output was still absent at one and ten
seconds. Another profile cycle restored it. The QEMU profile was restored
and the isolated daemon stopped after the experiment.

This reproduces persistent output loss after temporary contention. It does
not identify the original HDMI holder or reproduce the complete GDM handoff.

## Why releasing the device does not repair the output

In PipeWire 1.6.6's `spa/plugins/alsa/alsa-udev.c`,
`emit_added_object_info()` checks busy PCMs before marking the card emitted.
`process_card()` retries unavailable cards on permission/close notifications,
but only creates a card when `card->emitted` is false. A node creation failure
later in WirePlumber does not reset that card flag. Consequently, releasing
the PCM does not recreate the failed node of an already emitted card.

WirePlumber 0.5.14's `src/scripts/monitors/alsa.lua` logs the node activation
failure without retrying. Its D-Bus module defaults to the session bus
(`modules/module-dbus-connection.c`), as selected explicitly by the shipped
configuration's `support.dbus` component.

## Prevention candidates and remaining proof

1. **Restore seat-controlled hardware access.** Remove static `audio` group
   membership for wyrm2's operator account and rely on logind's active-seat
   ACLs. The current group membership grants device access even before a
   graphical login; the active session's named ACL grants that access when
   it is needed. This targets the observed pre-login overlap. It requires a
   fresh user manager/session to test: changing the group database does not
   remove supplementary groups from existing PipeWire processes. Validate
   physical login, monitor sleep/wake, and required remote/streaming use
   before treating it as a verified prevention.
2. **Handle failed node creation upstream.** Add bounded recovery after an
   ALSA node fails to bind, accounting for device removal and profile changes.
   The controlled reproduction is a regression case for this missing retry.
   WirePlumber 0.5.18 adds recovery for nodes that enter the error state after
   successful activation, but its activation failure callback still does not
   retry. An upgrade alone has not been shown to cover this failure.

No persistent configuration change was made. If the original conflict recurs,
capture `/proc/asound/card1/pcm3p/sub0/status` and privileged
`fuser -v /dev/snd/*` while it is busy, together with both sessions' audio logs,
before recreating the profile.
