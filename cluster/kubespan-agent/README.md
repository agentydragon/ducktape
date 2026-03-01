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

## Building

```bash
bazel build //cluster/kubespan-agent
```

## Configuration

Copy and edit the example config:

```bash
cp kubespan-agent.example.yaml /etc/kubespan/agent.yaml
```

Extract the cluster credentials from Talos:

```bash
# From a Talos node:
talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.id'
talosctl -n <node> get machineconfiguration -o yaml | yq '.spec.cluster.secret'

# Or from OpenTofu state:
cd cluster/terraform/bootstrap/infrastructure
tofu output -json talos_machine_secrets | jq -r '.cluster.id'
tofu output -json talos_machine_secrets | jq -r '.cluster.secret'
```

### Config Fields

| Field                | Default                           | Description                                                       |
| -------------------- | --------------------------------- | ----------------------------------------------------------------- |
| `cluster_id`         | (required)                        | Talos cluster identity (base64)                                   |
| `cluster_secret`     | (required)                        | 32-byte AES key for discovery encryption (base64)                 |
| `discovery_endpoint` | `discovery.talos.dev:443`         | gRPC discovery service endpoint                                   |
| `listen_port`        | `51820`                           | WireGuard UDP port                                                |
| `mtu`                | `1420`                            | WireGuard interface MTU                                           |
| `identity_file`      | `/var/lib/kubespan/identity.json` | Persisted WireGuard keypair path                                  |
| `force_routing`      | `false`                           | Route to peers even when they are down                            |
| `machine_type`       | `worker`                          | Advertised to discovery (`worker`/`controlplane`)                 |
| `extra_endpoints`    | `[]`                              | Additional endpoints to announce (e.g. `["203.0.113.1:51820"]`)   |
| `endpoint_filters`   | `[]`                              | CIDR allow/deny list (prefix `!` to deny, e.g. `["!10.0.0.0/8"]`) |
| `insecure_discovery` | `false`                           | Skip TLS verification for self-hosted discovery services          |

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

## Testing

```bash
# Unit tests:
bazel test //cluster/kubespan-agent:kubespan-agent_test

# E2E integration test (requires Docker):
bazel test //cluster/kubespan-agent/e2e:e2e_test
```

The E2E test starts a local discovery service, a Talos container, and kubespand in
discovery-only mode — verifying the encrypted gRPC discovery protocol works end-to-end.
All container images are managed as Bazel dependencies (no registry pulls at test time).

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

This daemon reimplements the following Talos controllers/adapters:

| kubespand file | Talos source                                                                                        |
| -------------- | --------------------------------------------------------------------------------------------------- |
| `identity.go`  | `internal/app/machined/pkg/adapters/kubespan/identity.go`, `pkg/machinery/resources/network/ula.go` |
| `discovery.go` | `internal/app/machined/pkg/controllers/cluster/discovery_service.go`                                |
| `wireguard.go` | `internal/app/machined/pkg/controllers/kubespan/manager.go`                                         |
| `routing.go`   | `internal/app/machined/pkg/controllers/kubespan/manager.go` (nftables + ip rules)                   |
| `peerstate.go` | `internal/app/machined/pkg/adapters/kubespan/peer_status.go`                                        |

Constants match `talos/pkg/machinery/constants/constants.go`:

- WireGuard port: 51820, MTU: 1420, keepalive: 25s
- Firewall marks: 0x20 (WG egress), 0x40 (force-route), 0x60 (mask)
- Routing table: 180, rule priority: 32500
- Peer down interval: 275s, endpoint connection timeout: 15s

## Limitations and Unimplemented Features

### Not Yet Implemented

- **Harvest extra endpoints**: Talos can learn additional endpoints from WireGuard handshake
  source addresses. Not implemented.
- **Advertise Kubernetes networks**: Talos controlplane nodes can advertise pod/service CIDRs
  as additional allowed IPs. Not implemented — only the node's own KubeSpan ULA /128 and any
  addresses published by the Talos peer are routed.
- **CSR auto-approval**: When running as a Kubernetes node, this daemon does not handle
  kubelet CSR approval (needs a separate mechanism).
- **NixOS/systemd module**: No packaged NixOS module or systemd unit yet.

### Known Differences from Talos

- **Single MAC detection**: Talos uses `net.FirstHardwareAddr()` from its own network
  stack. This daemon scans `/sys/class/net/` for the first physical NIC's MAC address,
  skipping loopback and virtual interfaces. The MAC is only used for EUI-64 address
  derivation.
- **No COSI resource model**: Talos uses its COSI controller-runtime for state management.
  This daemon uses a simpler imperative reconciliation loop.
- **Machine type is informational**: The `machine_type` config field is advertised to the
  discovery service but does not change behavior. Talos uses it for API endpoint selection
  (only controlplane nodes' endpoints are used for the Talos API).

### Security Considerations

- Requires the cluster secret in plaintext in the config file. Protect the config file
  (mode 0600, owned by root).
- The cluster secret is used as both the AES-GCM key for discovery encryption and the
  WireGuard preshared key — same as Talos.
- Runs as root (required for creating network interfaces and routing rules).
