# kubespand — KubeSpan Agent for Non-Talos Linux

A standalone Go daemon that joins a [Talos Linux](https://www.talos.dev/) KubeSpan WireGuard
mesh from non-Talos machines. Implements the same discovery + WireGuard + nftables protocol
that Talos nodes use, so non-Talos nodes appear as first-class KubeSpan peers.

Sidero Labs confirmed there is no standalone KubeSpan client
([discussion #10032](https://github.com/siderolabs/talos/discussions/10032)),
so this daemon fills that gap.

## How It Works

kubespand uses the [COSI](https://github.com/cosi-project/runtime) (Controller Runtime +
State Interface) framework — the same reactive resource/controller model that Talos uses
internally. Resources flow through an in-memory state, and controllers reconcile
automatically when their inputs change.

**Controllers:**

1. **IdentityController**: watches `Config` → produces `Identity` (WireGuard keypair +
   IPv6 ULA address derived from cluster ID + local MAC)
2. **DiscoveryController**: watches `Config` + `Identity` → produces `PeerSpec` resources
   by communicating with the Talos discovery service (`discovery.talos.dev:443`) via the
   official [discovery-client](https://github.com/siderolabs/discovery-client) library
3. **ManagerController**: watches `Config` + `Identity` + `PeerSpec` → produces
   `PeerStatus` resources, manages the `kubespan` WireGuard interface (preshared key,
   25s keepalive), nftables rules, and ip policy routing (table 180, fwmark 0x40/0x60).
   Polls handshake times every 30s and cycles endpoints for down peers (same state
   machine as Talos)

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

| kubespand file            | Talos source                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `resources.go`            | `pkg/machinery/resources/kubespan/*.go` (COSI resource definitions)                                                |
| `controller_identity.go`  | `internal/.../controllers/kubespan/identity.go` (IdentityController)                                               |
| `controller_discovery.go` | `internal/.../controllers/cluster/discovery_service.go`, `.../kubespan/peer_spec.go`                               |
| `controller_manager.go`   | `internal/.../controllers/kubespan/manager.go` (WireGuard + nftables + peer state)                                 |
| `identity.go`             | `pkg/machinery/resources/network/ula.go` (ULAPrefix), `internal/.../adapters/kubespan/identity.go` (EUI-64)        |
| `discovery.go`            | `internal/.../controllers/cluster/discovery_service.go` (discovery client adapter)                                 |
| `wireguard.go`            | `internal/.../controllers/kubespan/manager.go` (WireGuard device config)                                           |
| `routing.go`              | `internal/.../controllers/kubespan/manager.go` (nftables), `.../kubespan/routing_rules.go` (ip rules)              |
| `peerstate.go`            | `pkg/machinery/resources/kubespan/peer_status.go`, `internal/.../adapters/kubespan/peer_status.go` (state machine) |
| `config.go`               | `pkg/machinery/constants/constants.go` (KubeSpan\* constants)                                                      |

## Limitations

- **Harvest extra endpoints**: Talos learns additional endpoints from WireGuard handshake
  source addresses. Not implemented.
- **Advertise Kubernetes networks**: Talos controlplane nodes advertise pod/service CIDRs.
  Not implemented — only the node's own KubeSpan ULA is routed.
- **Single MAC detection**: Uses `/sys/class/net/<name>/device` sysfs probe (with fallback)
  instead of Talos's internal `FirstHardwareAddr` controller.
