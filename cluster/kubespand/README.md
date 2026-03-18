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
2. **DiscoveryController**: watches `Config` + `Identity` + `Endpoint` → produces
   `cluster.Affiliate` resources by communicating with the Talos discovery service
   (`discovery.talos.dev:443`) via the official
   [discovery-client](https://github.com/siderolabs/discovery-client) library. Harvested
   endpoints from the EndpointController are re-announced via the discovery service.
3. **PeerSpecController**: watches `Config` + `Identity` + `cluster.Affiliate` → produces
   `PeerSpec` resources. Applies endpoint filtering, builds AllowedIPs from affiliate data,
   and detects/resolves IP overlaps between peers.
4. **ManagerController** (imported from upstream Talos): watches `Config` + `Identity` +
   `PeerSpec` → produces `PeerStatus` resources, manages the `kubespan` WireGuard
   interface (preshared key, 25s keepalive), nftables rules, and ip policy routing
   (table 180, fwmark 0x40/0x60). Writes to `network.ConfigNamespaceName`; merge
   controllers (Address, Link, Route) bridge to `network.NamespaceName` where spec
   controllers read.
5. **EndpointController**: watches `Config` + `PeerStatus` + `cluster.Affiliate` →
   produces `Endpoint` resources for connected peers with valid endpoints. Enables
   endpoint harvesting for re-announcement via the discovery service.
6. **KubernetesNodeController** (optional): watches `Config` + K8s Node via client-go
   informer → produces `KubernetesNetworks` resource with PodCIDRs + ServiceCIDRs.
   Enabled when `advertise_kubernetes_networks: true`. The DiscoveryController reads
   this resource and includes the prefixes in `AdditionalAddresses` when publishing.
7. **KubePrismConfigController** (optional): watches `cluster.Affiliate` → produces
   `k8s.KubePrismConfig` with discovered control plane endpoints + configured fallback.
   Enabled when `kubeprism.enabled: true`.
8. **KubePrismController** (optional, embedded from Talos): watches `k8s.KubePrismConfig`
   → manages a TCP load balancer on `localhost:7445` that proxies to kube-apiserver
   endpoints. Provides the same local API server proxy that Talos nodes get natively
   via KubePrism built into `machined`.
9. **OSRootController** (optional): watches agent config → produces `secrets.OSRoot`
   with the Talos CA certificate and machine token. Enabled when `api.ca_crt` and
   `api.token` are configured.
10. **APICertSANsController** (optional, embedded from Talos): watches `secrets.OSRoot`
    - network addresses + hostname → produces `secrets.CertSAN` with certificate SANs.
11. **APIController** (optional, embedded from Talos): watches `secrets.OSRoot` +
    `secrets.CertSAN` + control plane endpoints → generates a CSR, sends it to a
    control plane node's trustd service (port 50001) for signing, and produces
    `secrets.API` with the signed certificate. This enables apid to serve mTLS
    on port 50000.

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

## Testing

```bash
bazel test //cluster/kubespand/qemu_tests/...
```

Integration tests boot lightweight Alpine Linux VMs under QEMU (TCG, no KVM required).
Full VMs are necessary because kubespand creates WireGuard interfaces, writes nftables
rules, and manipulates ip policy routing — operations that require a real kernel network
stack and root in an isolated environment. Docker containers don't work: nftables mark
expressions (`meta mark set`/`Bitwise`) return `EBUSY` deterministically on GHA runners
and in containers generally, even with `--network=none`. This is consistent with upstream
(Talos CI also uses QEMU VMs for KubeSpan tests, never containers).

Test topologies: flat (2 nodes, same subnet), cross-subnet (2 nodes, routed), and
double-NAT (3 nodes behind 2 separate NAT routers). Each VM runs a custom PID-1 init
that starts kubespand, waits for peer discovery, and runs ICMP + TCP connectivity probes.

## Architecture Reference

Maps to the following Talos source files:

| kubespand file / imported controller       | Talos source                                                                                            |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `controllers/kubespan/identity.go`         | `internal/.../controllers/kubespan/identity.go` (IdentityController)                                    |
| `controllers/cluster/discovery_service.go` | `internal/.../controllers/cluster/discovery_service.go` (DiscoveryServiceController)                    |
| (imported) `controllers/cluster`           | `internal/.../controllers/cluster/local_affiliate.go` (LocalAffiliateController)                        |
| (imported) `controllers/kubespan`          | `internal/.../controllers/kubespan/peer_spec.go` (PeerSpecController + endpoint filters)                |
| (imported) `controllers/kubespan`          | `internal/.../controllers/kubespan/manager.go` (ManagerController — WG interface, nftables, peer state) |
| `controllers/network/wireguard_link.go`    | `internal/.../controllers/network/link_spec.go` (WireGuard subset)                                      |
| `controllers/kubespand/config.go`          | (kubespand-only: YAML → COSI config injection)                                                          |
| `controllers/kubespand/node_metadata.go`   | (kubespand-only: produces shim COSI resources for LocalAffiliateController)                             |
| `controllers/cluster/kubernetes_node.go`   | (kubespand-only: K8s informer → `k8s.NodeStatus` for PodCIDRs)                                          |
| `controllers/k8s/kubeprism_config.go`      | `internal/.../controllers/k8s/kubeprism_endpoints.go` + `kubeprism_config.go` (adapted)                 |
| (imported) `controllers/k8s`               | `internal/.../controllers/k8s/kubeprism.go` (TCP LB manager)                                            |
| `controllers/kubespand/os_root.go`         | `internal/.../controllers/secrets/root.go` (RootOSController — adapted for YAML config)                 |
| (imported) `controllers/secrets`           | `internal/.../controllers/secrets/api.go` + `api_cert_sans.go` (APIController + APICertSANsController)  |
| (imported) `controllers/network`           | `internal/.../controllers/network/hardware_addr.go` (HardwareAddrController — first physical NIC MAC)   |
| `identity/identity.go`                     | `internal/.../adapters/kubespan/identity.go` (keypair persistence only; MAC via HardwareAddrController) |
| `discovery/discovery.go`                   | `internal/.../controllers/cluster/discovery_service.go` (discovery client wrapper)                      |
| `agentconfig/agentconfig.go`               | `pkg/machinery/constants/constants.go` (KubeSpan\* constants)                                           |
| `agentconfig/resource.go`                  | (kubespand-only: COSI resource for agent-specific config)                                               |

## Known Gaps

| Gap                          | Talos Reference                                | Our Approach                              |
| ---------------------------- | ---------------------------------------------- | ----------------------------------------- |
| `AffiliateMergeController`   | Merges raw → cluster namespace affiliates      | Skipped (single source)                   |
| `MachineResetSignal` cleanup | DiscoveryServiceCtrl cleans up on reset        | Not implemented                           |
| `ConfigController`           | Reads `MachineConfig` → `ConfigSpec`           | We inject from YAML                       |
| Multiple identity sources    | Talos uses STATE partition + `HardwareAddr`    | We use flat file + HardwareAddrController |
| Full `LinkSpecController`    | Handles bonds, bridges, VLANs, WG (~700 lines) | WireguardLinkController (WG only)         |
