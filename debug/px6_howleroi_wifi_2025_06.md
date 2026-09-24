# Rai-PX6 — WiFi connectivity drops at Howleroi (May–Jun 2025)

**Status: resolved / no longer reproducing (confirmed Aug 2026).** Raw diagnostics were
discarded after this note was written; only these conclusions remain.

## Symptom

`Rai-PX6` (Android device, MAC `dc:e5:5b:17:f9:45`, DHCP lease `10.0.3.51` on `br0`)
repeatedly lost network connectivity at the Howleroi (SF) flat, behind `wolf-gateway`
(UniFi / UbiOS). Observed 2025-05-20 → 2025-06-06.

## Evidence gathered

- UniFi support bundle `support-9065-1749235872197` (gateway `/var/log`, ~931 MB)
- `2025-06-06-wolf-gateway-support.tgz`, `2025-06-06-wolf-gateway-var-log-messages.gz`
- `wifi_events-2025-06-04-1113.txt` — Android **logcat** events buffer from the device
  (`am_cpu`, `roid.apps.tycho` / Google Fi, `cp2ap_wakeup_wq` modem wakeups)
- `matches.txt` — own grep of the bundle for the device MAC

## Findings

**1. Repeated full DHCP re-acquisition, not lease renewal.** 7 × `DHCPDISCOVER` (the client
had lost state entirely, rather than renewing) against 26 × `DHCPREQUEST`/`DHCPACK`.
Clustered: 06-01 ×1, 06-03 ×1, **06-04 ×3**, 06-06 ×2. Each DISCOVER→OFFER→REQUEST→ACK
completed in <1s, so the gateway's DHCP service was healthy — the client was re-associating.

**2. UniFi DPI could not keep up with this MAC.** 41 ×
`ubnt-dpi-util: pcap dumping rate exceeded` for `dc:e5:5b:17:f9:45`, with dump success
degrading over the window (~21% → ~17%). Correlated with the drops; never established as
cause. UniFi DPI/"Traffic Identification" is a known source of client instability and is
the first thing to disable if this recurs.

**3. DPI device fingerprinting was consistently wrong**, guessing `Google Pixel 9 Pro Fold`
@22%, `Pixel 6 Pro` @15% etc. Cosmetic, but it means UniFi client-type filters were
unreliable for this device.

## If it recurs

1. Disable DPI / Traffic Identification on `wolf-gateway` and re-test.
2. Watch for `DHCPDISCOVER` (not `DHCPREQUEST`) in gateway `daemon.log` — DISCOVER means the
   client dropped association, which points at the WiFi layer, not DHCP.
3. Pull logcat from the device at the same timestamps; the gateway logs alone don't show the
   disassociation reason.

## Related

- `debug/atlas/ethernet_recurring/README.md` — separate investigation, but documents the
  same `wolf-gateway` topology (dumb switch → gateway Port 2) and its "Wired Client
  Disconnected" event stream.
