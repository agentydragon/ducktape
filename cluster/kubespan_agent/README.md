# kubespand — KubeSpan Agent for Non-Talos Linux

A standalone Go daemon that joins a [Talos Linux](https://www.talos.dev/) KubeSpan WireGuard
mesh from non-Talos machines. Implements the same discovery + WireGuard + nftables protocol
that Talos nodes use, so non-Talos nodes appear as first-class KubeSpan peers.

Sidero Labs confirmed there is no standalone KubeSpan client
([discussion #10032](https://github.com/siderolabs/talos/discussions/10032)),
so this daemon fills that gap.

## How It Works

1. **Identity**: generates a WireGuard keypair and derives an IPv6 ULA address from the
   cluster ID + local MAC (same algorithm as Talos)
2. **Discovery**: connects to the Talos discovery service (`discovery.talos.dev:443`) via
   the official [discovery-client](https://github.com/siderolabs/discovery-client) library,
   announces itself and watches for peers
3. **WireGuard**: creates a `kubespan` WireGuard interface, configures peers with
   preshared key (cluster secret) and 25s keepalive
4. **Routing**: installs nftables rules and ip policy routing to steer matching traffic
   through the WireGuard tunnel (table 180, fwmark 0x40/0x60)
5. **Peer health**: polls WireGuard handshake times every 30s, cycles endpoints when a
   peer goes down (same state machine as Talos)

## Prerequisites

- Linux (kernel 5.6+ for WireGuard)
- `wireguard` kernel module loaded
- Root privileges (creates network interfaces and routing rules)

## Configuration

See `kubespand.example.yaml` for all config fields and documentation. Extract cluster
credentials from Talos:

```bash
talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.id'
talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.secret'
```

## Running

```bash
sudo ./kubespand -config /etc/kubespan/agent.yaml
```

On first run, it generates a WireGuard keypair at the configured `identity_file` path. The
keypair is reused on subsequent runs to maintain a stable KubeSpan identity.

### Discovery-Only Mode

Run without WireGuard/routing to test discovery connectivity:

```bash
./kubespand -discovery-only -timeout 60s -config /etc/kubespan/agent.yaml
```

Exits 0 when at least one peer is discovered, 1 on timeout. No root required.

### Signal Handling

kubespand handles `SIGTERM` and `SIGINT` gracefully: it deregisters from the discovery
service, removes nftables rules, ip policy routing rules, and the WireGuard interface
before exiting.

## Verifying

```bash
# Check WireGuard interface and peers:
wg show kubespan

# Check ip rules:
ip rule show | grep 32500

# Check routing table 180:
ip route show table 180

# From a Talos node, verify the non-Talos peer is visible:
talosctl get kubespanpeerstatuses
```

## Architecture Reference

Maps to the following Talos source files:

| kubespand file | Talos source                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `identity.go`  | `pkg/machinery/resources/network/ula.go` (ULAPrefix), `internal/.../adapters/kubespan/identity.go` (EUI-64)                    |
| `discovery.go` | `internal/.../controllers/cluster/discovery_service.go`                                                                        |
| `wireguard.go` | `internal/.../controllers/kubespan/manager.go` (WireGuard device config)                                                       |
| `routing.go`   | `internal/.../controllers/kubespan/manager.go` (nftables), `internal/.../controllers/kubespan/routing_rules.go` (ip rules)     |
| `peerstate.go` | `pkg/machinery/resources/kubespan/peer_status.go` (PeerState), `internal/.../adapters/kubespan/peer_status.go` (state machine) |
| `config.go`    | `pkg/machinery/constants/constants.go` (KubeSpan\* constants)                                                                  |

## Limitations

- **Harvest extra endpoints**: Talos learns additional endpoints from WireGuard handshake
  source addresses. Not implemented.
- **Advertise Kubernetes networks**: Talos controlplane nodes advertise pod/service CIDRs.
  Not implemented — only the node's own KubeSpan ULA is routed.
- **No COSI resource model**: Uses a simpler imperative reconciliation loop.
- **Single MAC detection**: Uses `/sys/class/net/<name>/device` sysfs probe (with fallback)
  instead of Talos's internal `FirstHardwareAddr` controller.
