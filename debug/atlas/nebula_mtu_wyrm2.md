# Nebula MTU missing on wyrm2 — 2026-07-03

## Symptom

After wyrm2 rebooted at ~2026-07-03 02:40Z (kernel 6.12.83 → 6.12.93), pods on
wyrm2 could not establish TCP connections to most OVH-hosted cluster services
(mimir-gateway, tofu-state-db Postgres). Small packets succeeded (DNS, API
server 401, SYN/ACK); large data transfers timed out.

## Root cause

`nebula1` MTU was **1300** (Nebula's default). The cluster network stack
requires `nebula1 = 1420` (see `cluster/docs/network.adoc`):

```
pod 1370 + VXLAN 50 = 1420 fits nebula1   (1420 + 60 Nebula = 1480 fits eno1 1500)
```

With nebula1 at 1300, the kernel rejected any VXLAN frame larger than 1300 bytes
with "Message too long" (DF-set send). Cilium's cross-node pod MTU is 1370, so
every cross-node packet of non-trivial size was dropped.

**Confirmed**:

```
ping -M do -s 1260 10.42.0.13  →  OK    (1288 bytes < 1300 MTU)
ping -M do -s 1372 10.42.0.13  →  FAIL  "Message too long" (1400 bytes > 1300)
```

## Why it was missing

`nix/nixos/modules/nebula.nix` generated the Nebula config JSON from Nix. The
`tun` block only had `dev = "nebula1"` — no `mtu` field. Nebula defaults to
1300 when `tun.mtu` is absent.

This was a **pre-existing bug**, not caused by the reboot. The reboot (a clean
`nixos-rebuild switch`) did not change the Nix config; it just restarted Nebula,
which re-applied the 1300 default. The previous 36-hour boot (kernel 6.12.83)
had the same MTU problem.

## Fix

Added `mtu = 1420;` to the `tun` block in `nebula.nix`:

```nix
tun = {
  dev = "nebula1";
  mtu = 1420;   # must match Cilium underlay MTU; Nebula default 1300 drops VXLAN frames
};
```

To apply without a full rebuild:

```bash
sudo python3 -c "
import json
with open('/etc/nebula/config.yaml') as f:
    c = json.load(f)
c['tun']['mtu'] = 1420
with open('/etc/nebula/config.yaml', 'w') as f:
    json.dump(c, f)
"
sudo systemctl restart nebula
ip link show nebula1   # verify mtu 1420
```

Permanent: `sudo nixos-rebuild switch`.

## Secondary: Nebula handshake failures to 10.42.0.16/10.42.0.17

`ovh-ns104952` (10.42.0.16, 147.135.104.5) and `ovh-ns104963` (10.42.0.17,
147.135.104.16) — the KS-GAME OVH workers — are configured as lighthouses/relays
in `nebula-mesh.json` but Nebula on wyrm2 cannot complete handshakes to them.

- Physical host IPs are reachable (`ping 147.135.104.5` works).
- Both K8s nodes show `Ready=True` with recent heartbeats.
- Handshakes send, get no response, time out (~6.5 s), retry indefinitely.

This is a separate issue (possibly UDP/4242 not open on those hosts, or Nebula
extension misconfiguration). Pods on those nodes are reachable through relay via
the lighthouses (10.42.0.13–10.42.0.15) but this adds latency.

Not the cause of the outage described above — that's entirely the MTU bug.

## Other findings

- **Reboot cause**: Clean voluntary shutdown at 19:33 local (2026-07-02). Not OOM,
  kernel panic, or thermal. A deliberate `nixos-rebuild switch` that upgraded the
  kernel from 6.12.83 to 6.12.93.
- **High DaemonSet restart counts** (nvidia-device-plugin 1260, proxmox-csi-node 817
  over 82 days): proportional to reboots; each reboot increments all DaemonSet
  restart counters. Not a crash loop.
- **Current journal errors**: only `virtio_gpu` display driver noise (cosmetic,
  from the Proxmox virtual display) and sudo auth denials (no password in
  headless sessions).
