# Hetzner Auction Servers as K8s Nodes

## Idea

Buy dedicated servers from the Hetzner Robot auction market and join them to the
cluster as Talos worker nodes, alongside the existing Hetzner Cloud VPS and Proxmox
nodes.

## Why

Auction servers can be significantly cheaper per CPU/RAM/storage than Cloud VPS for
baseline capacity — especially for memory- or storage-heavy workloads. The auction
market lists servers with fixed prices, so you can watch for good deals on specific
specs.

## Hetzner Robot API

The auction market is accessible via the Robot REST API at
`https://robot-ws.your-server.de/order/server_market/product` (note: underscore, not
slash), with HTTP Basic Auth. `hcloud` CLI and tokens do not work — Robot is a
completely separate product with its own credentials. Even with 2FA on the main
account, the API uses a dedicated webservice sub-user (no 2FA) created in Robot account
settings. Credentials are stored in `secrets/hetzner-robot.sops.yaml`
(`username` + `password`, only `password` is encrypted).

## Geographic Availability

**Robot auction servers are Europe-only.** As of May 2026, all 312 listed servers are
in FSN1 (Falkenstein), HEL1 (Helsinki), or NBG1 (Nuremberg). HIL (Hillsboro, OR) is a
Hetzner Cloud-only datacenter — no dedicated/auction servers exist there.

This is the key blocker for using auction servers as drop-in replacements for the
current HIL VPS workers. EU servers would be ~130ms from US West vs. <5ms for HIL,
which matters for etcd latency if mixed into the control plane.

## Price Comparison (May 2026)

Current HIL CPX31 workers: €15.90/mo each (4 vCPU, 8GB RAM, 160GB SSD).

Cheapest EU auction servers at the time of checking:

| Price  | DC   | CPU            | RAM  | Storage        |
| ------ | ---- | -------------- | ---- | -------------- |
| €37/mo | FSN1 | i7-6700        | 32GB | 2×480GB DC SSD |
| €38/mo | FSN1 | i7-7700        | 64GB | 2×250GB SSD    |
| €43/mo | HEL1 | Ryzen 5 3600   | 64GB | 2×2TB ENT HDD  |
| €43/mo | FSN1 | Xeon E3-1275v5 | 64GB | 2×480GB DC SSD |

At €43/mo you get 64GB RAM — 8× more than a CPX31 for 2.7× the price. Strong value
for memory-heavy workloads, but only viable if EU latency is acceptable for the
workload in question.

## What Would Change

### Unchanged

- Talos machine config generation (`talos_machine_configuration` data source)
- Talos machine secrets and PKI
- `talos_machine_configuration_apply` / bootstrap (same provider, different IP)
- Nebula mesh overlay (add a node entry, same as any other node)
- Cilium, Flux, everything above the OS layer

### New wiring needed

**Terraform provider**: `hcloud` is Cloud-only. Robot dedicated servers need the
community provider `hetznercloud/hrobot` (or scripted Robot API calls). A new entry
in `terraform.tf`'s providers block.

**OS installation**: Existing flow uses Packer to build a Cloud snapshot (rescue boot
→ `dd` Talos → snapshot → create server). Robot has no snapshot mechanism. Instead:
Robot API activates rescue mode → reboot into Debian rescue → `dd` Talos image →
reboot. Essentially the same `dd` trick, driven via `hrobot_boot` (rescue activation)

- `hrobot_reset` (reboot) resources, or a `null_resource` provisioner. The Image
  Factory schematic and image URL would be the same.

**Firewall**: `hcloud_firewall` does not apply to Robot servers. Use `hrobot_firewall`
(different API, different rule syntax) or rely on Talos nftables + Nebula (which may
be sufficient — the hcloud firewall is mainly useful for the initial Talos API
exposure window).

**Node labels**: Talos CCM reads hcloud metadata to set topology labels. Robot servers
have no hcloud metadata. Labels would need to be set explicitly in the Talos machine
config patches.

**Server acquisition**: Cannot be automated in TF — buying from the auction is a
one-time manual click. The server then lands in the Robot account with a fixed server
ID and static IP. TF takes over from that point.

## Estimated Scope

Probably 1–2 new `.tf` files; no changes to the existing infra or node logic beyond
adding the new node entries. The Talos + Nebula + k8s layers are essentially free.
The work is the Robot provisioning plumbing (provider, rescue-mode install, firewall).
The community `hrobot` provider is less mature than the official `hcloud` provider,
which adds some risk.

## Open Questions

- Are there specific spec/price targets that would make this worthwhile vs. adding
  Cloud VPS?
- Is the `hrobot` community provider maintained well enough to rely on, or is a
  scripted approach safer?
- Rescue-mode install is inherently more stateful than a snapshot-based approach —
  how to handle re-provisioning if the drive needs to be wiped?
- Robot firewall vs. relying on Nebula + Talos nftables: which is preferable?
