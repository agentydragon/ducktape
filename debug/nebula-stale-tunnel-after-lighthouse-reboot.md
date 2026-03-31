# Nebula: Stale Tunnel After Lighthouse Reboot

## Status: Under investigation

## Problem

When both nebula lighthouses are rebooted (new session keys), non-lighthouse peers
never re-establish tunnels. The peer's nebula process believes the tunnels are still
alive and continues sending encrypted packets with old session keys. The lighthouses
silently drop these packets (wrong session), but the peer never detects the failure.

## Symptoms

- `ping` to lighthouse nebula IPs: 100% packet loss
- Nebula logs: `Attempt to relay through hosts [10.42.0.1 10.42.0.2]` (thinks tunnels
  are alive, tries to relay through them)
- No `Handshake message sent` to lighthouses — nebula isn't trying to re-handshake
- `Refusing to handshake with myself` errors (NAT hairpin, unrelated noise)
- TX dropped on `nebula1` tun interface: billions of packets

## Impact

This caused a cluster outage on 2026-03-30. VPS lighthouse nodes were rebooted to
recover from OOM. Nebula on wyrm2 (non-lighthouse) didn't detect the stale tunnels.
Cross-node pod networking was dead. Took ~3 hours to identify as a nebula issue
(initially attributed to Cilium/etcd).

## Repro

1. Have a working nebula mesh with 2 lighthouses + 1 non-lighthouse peer
2. Reboot both lighthouses (or restart nebula on both)
3. Observe: the non-lighthouse peer never re-handshakes with the lighthouses
4. The mesh is dead until the peer's nebula is also restarted

## Source Code Analysis (nebula v1.10.3)

Nebula has **two** mechanisms that should auto-recover when a peer restarts. Analysis
of the source code shows both should trigger within ~20 seconds. The question is why
neither worked.

### Mechanism 1: RecvError packets

When the lighthouse restarts, its hostmap is empty. When it receives an encrypted
packet from a client:

1. `readOutsidePackets()` calls `f.hostMap.QueryIndex(h.RemoteIndex)` → `nil`
   (lighthouse has no index entries after restart) (`outside.go:47`)
2. `ci = nil` since `hostinfo == nil` (`outside.go:50-53`)
3. `handleEncrypted(ci=nil, ...)` sends an unauthenticated `RecvError` packet back
   to the client and returns false (`outside.go:272-278`)
4. Client receives RecvError → `handleRecvError()` validates the source address,
   looks up the tunnel via `QueryReverseIndex`, calls `closeTunnel()` to delete the
   hostinfo, and clears the handshake manager entry (`outside.go:563-591`)
5. Next outbound packet → `GetOrHandshake()` finds no tunnel → initiates fresh
   Noise IX handshake (`inside.go:237`)

Default config: `send_recv_error: always`, `accept_recv_error: always` (both correct
for our deployments — we don't override these).

### Mechanism 2: Connection manager test-packet timeout

The `SendUpdate()` goroutine runs every `lighthouse.interval` seconds (10s in our
config), sending `HostUpdateNotification` to all lighthouses via
`SendMessageToVpnAddr()` (`lighthouse.go:823-847, 849-959`). Each send calls
`connectionManager.Out(hostinfo)` (`inside.go:360`), marking outbound traffic.

The connection manager runs `doTrafficCheck()` every `connection_alive_interval`
(default 5s) for each tracked tunnel (`connection_manager.go:162-171`):

1. **T+5s**: Detects outbound-only traffic (updates sent, no responses received).
   Sends a `Test` packet and sets `pendingDeletion = true`
   (`connection_manager.go:413-429`)
2. **T+15s**: `pendingDeletion` still true, no inbound traffic → `deleteTunnel`
   (`connection_manager.go:376-382`)
3. **T+15s+**: Next `SendUpdate()` call → `GetOrHandshake()` finds no tunnel →
   fresh handshake

### Root cause hypotheses

Both mechanisms should produce recovery within ~20 seconds. Why didn't they?

**H1 (most likely): RecvError UDP packet loss, combined with connection manager gap**

RecvError is a single unauthenticated UDP packet with **no retry**. If lost (common
with UDP, especially during lighthouse startup when OS buffers are filling and
iptables rules may not be fully loaded), the client never receives the "session
invalid" signal.

The connection manager fallback (mechanism 2) should still work. But its detection
depends on the lighthouse tunnel being registered in the traffic timer wheel. If
there's a code path where the timer entry is consumed and not re-added before the
lighthouse restarts, the connection manager would never check this tunnel again until
new traffic triggers re-registration.

**H2: Interaction between relay state and tunnel deletion**

Logs show `Attempt to relay through hosts [10.42.0.1 10.42.0.2]`. Our config has
`use_relays: true` with lighthouses as relays. When the client tries to reach other
peers via relay, it generates outbound traffic through the lighthouse tunnel. This
relay traffic also marks `Out(hostinfo)`, which could interact with the connection
manager's traffic tracking. If relay attempts keep refreshing the timer without the
test-packet flow completing properly, the tunnel might never reach `deleteTunnel`.

**H3: Both lighthouses down simultaneously creates a bootstrap deadlock**

When both lighthouses are down simultaneously, all outbound traffic fails. When they
come back, the client should start sending `SendUpdate` again, which should trigger
mechanism 2. However, if the `SendUpdate` goroutine's `GetOrHandshake()` call during
the outage period cached failed handshake state that persists after the lighthouses
return, it could delay recovery.

**H4: Talos extension service nebula version / config**

The Talos nebula extension may bundle an older nebula version with different behavior.
The `accept_recv_error` config option was only added in v1.10.1. If the Talos
extension uses an older version, RecvError acceptance might behave differently.

### What the code gets right

- `handleEncrypted()` correctly sends RecvError when `ci == nil` for ALL message
  types including `LightHouse`, `Test`, and `Message` (`outside.go:57, 135, 154`)
- `handleRecvError()` correctly validates the source address matches the known remote
  before tearing down the tunnel (`outside.go:583-586`)
- The connection manager correctly distinguishes "outbound-only" (dead peer) from
  "no traffic" (idle tunnel) (`connection_manager.go:386-404`)
- `SendUpdate()` generates periodic outbound traffic to lighthouses, keeping the
  connection manager engaged (`lighthouse.go:837`)

### What the code gets wrong (potential upstream issues)

- RecvError has **no retry** — a single lost UDP packet means the fast-recovery path
  fails entirely
- RecvError is unauthenticated — any packet from the right IP can trigger tunnel
  teardown, making it a DoS vector (mitigated by address check at `outside.go:583`)
- No built-in "lighthouse liveness" check — the client has no way to distinguish
  "lighthouse is up but tunnel is stale" from "lighthouse is down"
- `punchy` only maintains NAT state, not session liveness — `punch: true` and
  `respond: true` don't help detect stale sessions

## Diagnostic Logging

We had no useful nebula logs during the 2026-03-30 incident because logging was at
default level with no structured output configured.

### Key log messages to look for

| Log message                                   | Source     | Meaning                                                        |
| --------------------------------------------- | ---------- | -------------------------------------------------------------- |
| `Recv error sent`                             | Lighthouse | Lighthouse sent RecvError to a client (good)                   |
| `Recv error received`                         | Client     | Client received RecvError, tearing down tunnel                 |
| `Someone spoofing recv_errors?`               | Client     | RecvError from unexpected address — indicates address mismatch |
| `Tunnel status` `state=dead method=active`    | Client     | Connection manager deleted a dead tunnel                       |
| `Tunnel status` `state=alive method=passive`  | Client     | Tunnel has bidirectional traffic (healthy)                     |
| `Tunnel status` `state=testing method=active` | Client     | Sending test packet, no inbound traffic detected               |
| `Handshake message sent`                      | Client     | New handshake initiated (recovery starting)                    |
| `Failed to decrypt lighthouse packet`         | Either     | Decrypt failure — confirms stale session keys                  |
| `Failed to decrypt test packet`               | Either     | Test packet with stale keys                                    |
| `Handshake timed out`                         | Client     | Handshake failed after retries                                 |
| `Close tunnel received`                       | Either     | Graceful tunnel teardown                                       |

### Logging configuration added to all deployments

```yaml
logging:
  level: info
  format: json
```

Set `level: debug` during incidents to see connection manager decisions, RecvError
send/receive, and handshake state transitions. For Talos nodes, temporarily patch the
nebula extension config:

```bash
# On Talos, edit the nebula config via talosctl:
talosctl -n <node> edit extensionserviceconfig nebula
# Change logging.level to "debug", save
# Nebula will reload the config (SIGHUP) without restart
```

For NixOS nodes (wyrm2, rugged):

```bash
# Temporarily override logging level:
sudo systemctl stop nebula@config
# Edit /etc/nebula/config.yaml, set logging.level: debug
sudo systemctl start nebula@config

# Or rebuild with the change and switch:
sudo nixos-rebuild switch
```

For Ansible-managed nodes (atlas):

```bash
# Edit /etc/nebula/config.yaml directly for temp debug:
sudo sed -i 's/level: info/level: debug/' /etc/nebula/config.yaml
sudo systemctl reload nebula@config  # SIGHUP triggers config reload
```

### Verifying RecvError flow

To confirm RecvError is working between two nodes:

```bash
# On lighthouse, watch for RecvError sends:
journalctl -u nebula -f | grep -i "recv.error"

# On client, watch for RecvError receives and tunnel state:
journalctl -u nebula -f | grep -iE "recv.error|tunnel.status|handshake"
```

### Checking nebula version

```bash
# Talos:
talosctl -n <node> service nebula | grep -i version
# Or check the binary:
talosctl -n <node> read /usr/local/bin/nebula | head -c 1000 | strings | grep -i version

# NixOS:
nebula -version

# Ansible-managed:
/usr/local/bin/nebula -version
```

Verify all nodes run v1.10.1+ (which added `accept_recv_error` config option).

## Workaround

Restart nebula on all non-lighthouse peers after lighthouse reboot:

```bash
# NixOS (wyrm2, rugged):
sudo systemctl restart nebula@config

# Talos (pve-cp-0):
talosctl -n <node> service nebula restart
```

## TODO

- [ ] Add `logging` and `timers` config to all nebula deployments (Terraform, NixOS,
      Ansible, k8s) — done in this commit
- [ ] Verify nebula version on all nodes is ≥1.10.1
- [ ] File upstream issue at <https://github.com/slackhq/nebula/issues> with:
  - Repro steps (restart both lighthouses, observe client never re-handshakes)
  - Source code analysis showing RecvError has no retry
  - Request for: (a) RecvError retry, (b) connection manager guaranteed lighthouse
    liveness check, or (c) periodic re-handshake for lighthouse tunnels
- [ ] Reproduce in a controlled environment (QEMU or Docker) with debug logging to
      confirm which hypothesis is correct
- [ ] Consider a systemd watchdog timer that pings lighthouse nebula IPs and restarts
      nebula if unreachable for >60s (defense in depth)
