# Home network connectivity outage (2026-09-17)

## Status

Unconfirmed. Internet connectivity was restored after physically relaxing a tightly
looped fiber connector at the AT&T gateway, but no hard telemetry (optical signal
level, gateway event log, host-side network logs) was captured to confirm that was
actually the fault versus coincidental timing.

## Timeline

- ~4 days prior: `optiplex` physically relocated into the wiring closet that also
  houses the AT&T gateway.
- ~1 week prior: separate incident took down `wyrm2` + `atlas`; blamed on a Zyxel
  switch previously sitting between `atlas` and the gateway. The switch has since
  been removed (both hosts now cabled directly into the gateway).
- Day of: `wyrm2` and `optiplex` reported "flaking" (intermittent connectivity)
  through the day. `wyrm2` last confirmed online ~16:24 local (a Telegram sync).
- `ping google.com` on `wyrm2` failed with `System error` — glibc's
  `gai_strerror(EAI_SYSTEM)`, meaning `getaddrinfo()` failed at the socket layer
  (consistent with no route to the configured DNS server, not a DNS-name failure).
  `ip addr` showed `ens18` up, `192.168.1.72/24`, no default route confirmed at the
  time.
- Phone briefly failed to auto-join home WiFi; self-resolved a few minutes later.
- AT&T gateway status page (`192.168.1.254`) showed Broadband "Up", both WiFi bands
  enabled, and `atlas`/`wyrm2`/`optiplex` all listed "on" via Ethernet in the device
  list.
- Shortly after, the same page flipped to "Internet connection lost — No Ethernet
  cable detected for external ONT port (Error Code: NAD-622)".
- Inspection found the gateway's integrated fiber (SC/APC) connector routed through
  a tight loop (~3cm diameter / ~1.5cm bend radius), forced by the gateway's port
  panel sitting immediately adjacent to the closet's power outlets.
- SSH to `wyrm2` (over Nebula) failed (`ssh_connection_failed`) while the gateway was
  mid-resync.
- Loop relaxed to a wider bend, connector reseated. Gateway cycled flashing white →
  flashing red (fault persisted).
- A nearby power plug was removed to give the fiber cable a wider routing path.
  Gateway cycled again (flashing white, Ethernet ports re-linking) and came up;
  Broadband confirmed restored, `atlas` confirmed to have internet.
- A second SSH attempt to `wyrm2` (over Nebula) still failed (`ssh_connection_failed`)
  even after `atlas` had internet back. `wyrm2` was later confirmed up via a local
  check (not captured here).
- A follow-up check queued against `atlas` (uplink NIC carrier state, the `wyrm2`
  Proxmox VM's status, and a direct LAN ping to `wyrm2`) was not completed before
  this note was written.

## Leading hypothesis (unconfirmed)

The gateway's fiber patch cable's tight bend radius degraded the optical signal to
the point of a hard "no signal" fault, plausibly worsened by the cabling disturbance
from moving `optiplex` into the same closet. Relaxing the bend empirically fixed it,
but nothing was measured to confirm signal degradation was the actual mechanism:

- No optical Rx/Tx power reading was pulled from the gateway (if it exposes one).
- No gateway event/system log was pulled to line up loss-of-signal events against
  the physical changes.
- Neither `wyrm2` nor `atlas` network logs were captured — both SSH attempts during
  the outage failed.
- Not ruled in or out: whether this is the same underlying issue as last week's
  Zyxel-switch-blamed incident.

## Next steps

- Find out whether the AT&T BGW320-500 exposes historical telemetry (event log,
  optical Rx/Tx power, uptime) via its web UI, an API, or SNMP, and whether a
  collector (Home Assistant integration, Prometheus exporter, or similar) already
  exists for this model. This is the main gap keeping this incident at "probably"
  instead of "confirmed."
- Once reachable, pull `wyrm2`'s and `atlas`'s NetworkManager/kernel journal for the
  outage window (both live SSH attempts during the incident failed with
  `ssh_connection_failed`; unclear whether that was `wyrm2`-side, Nebula-side, or
  just bad timing against the gateway reboot).
- Physical follow-up once telemetry confirms or rules out the bend-radius theory:
  smaller power strip for the closet, and/or reorienting the gateway, and/or
  cable-management brackets — something to stop the fiber's slack from being forced
  through the same tight space next to the power outlets.

## Related

- `cluster/docs/lessons_learned/nebula_dns_defaultroute_breaks_host.md` — `wyrm2`'s
  `ens18` is its only default-route/DNS path (DNS 1.1.1.1/1.0.0.1); explains why
  losing that route surfaces as `ping`'s `System error` rather than a DNS-name
  failure.
- `debug/atlas/ethernet_recurring/README.md` — prior, similarly-shaped incident: a
  bad self-crimped cable on `atlas`'s own uplink caused a month of flapping before
  total failure. Flagged there as possibly stale since the cable was touched again
  for a GPU install.
- `cluster/terraform/main/proxmox-vms.tf`, `ansible/atlas.yaml` — `wyrm2` is a
  Proxmox VM on `atlas` (`vm_id=110`), bridged (`vmbr0`) onto `atlas`'s single
  physical uplink (`enp11s0`); it has no static network config of its own, so its
  entire network path rides that one link.
