# Nebula: stale tunnels after a lighthouse reboot

After **both** Nebula lighthouses reboot (e.g. recovering from an OOM), non-lighthouse
peers can get stuck believing their lighthouse tunnels are still alive: they keep
sending packets encrypted with stale session keys that the restarted lighthouses
silently drop, and never re-handshake. The mesh stays dead until each peer's Nebula is
restarted. This caused a ~3h cluster outage on 2026-03-30 (initially blamed on
Cilium/etcd).

## Spot it

Cross-node pod networking is dead while hosts look otherwise fine. On a non-lighthouse
peer:

- `ping <lighthouse nebula IP>` → 100% packet loss
- Nebula logs repeat `Attempt to relay through hosts [...]` (it thinks the tunnels are
  up) with **no** `Handshake message sent` to the lighthouses
- `Failed to decrypt lighthouse packet` / `Failed to decrypt test packet` confirm
  stale session keys

The tell: tunnels reported alive, zero handshakes, decrypt failures — the peer isn't
even trying to re-handshake.

## Recover

Restart Nebula on **every non-lighthouse peer** (the lighthouses are already fresh):

```bash
# NixOS (wyrm2, rugged, iguana):
sudo systemctl restart nebula@config

# Talos (control-plane + worker nodes):
talosctl -n <node> service nebula restart
```

Confirm a fresh handshake and bidirectional traffic:

```bash
journalctl -u nebula -f | grep -iE "handshake|tunnel.status"            # NixOS
talosctl -n <node> logs ext-nebula | grep -iE "handshake|tunnel.status"  # Talos
# expect `Handshake message sent` then `Tunnel status state=alive method=passive`
```

## Why

Nebula's RecvError fast-recovery path is a single unauthenticated UDP packet with no
retry, and neither of its two auto-recovery mechanisms fired here. Full source-code
analysis, hypotheses, and the diagnostic log-message table live in
<../../../debug/nebula-stale-tunnel-after-lighthouse-reboot.md>.

## Prevention

Avoid rebooting both lighthouses at once when possible. Open defense-in-depth idea
(tracked in the debug note above): a watchdog that pings lighthouse Nebula IPs and
restarts Nebula if unreachable for >60s.
