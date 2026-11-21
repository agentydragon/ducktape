# Hetzner auction servers as k8s nodes

Idea: buy dedicated servers on the Hetzner Robot auction (fixed prices; cheaper per CPU/RAM/storage than Cloud VPS for baseline capacity, especially memory- or storage-heavy) and join them as Talos workers beside the Cloud VPS and Proxmox nodes.

**Blocker: Robot auction servers are Europe-only.** As of May 2026 all 312 listings are in FSN1 (Falkenstein), HEL1 (Helsinki) or NBG1 (Nuremberg); HIL (Hillsboro, OR) is Cloud-only. EU is ~130 ms from US West vs <5 ms for HIL, which matters for etcd latency if mixed into the control plane.

## Robot API

Auction market: `https://robot-ws.your-server.de/order/server_market/product` (underscore, not slash), HTTP Basic Auth. `hcloud` CLI and tokens do not work: Robot is a separate product with its own credentials, a webservice sub-user (no 2FA, even with 2FA on the main account) from the Robot account settings. Stored in `secrets/hetzner-robot.sops.yaml` (`username` + `password`; only `password` is encrypted).

## Prices (May 2026)

HIL CPX31 workers: €15.90/mo each (4 vCPU, 8 GB, 160 GB SSD). Cheapest EU auction servers:

| Price  | DC   | CPU            | RAM  | Storage        |
| ------ | ---- | -------------- | ---- | -------------- |
| €37/mo | FSN1 | i7-6700        | 32GB | 2×480GB DC SSD |
| €38/mo | FSN1 | i7-7700        | 64GB | 2×250GB SSD    |
| €43/mo | HEL1 | Ryzen 5 3600   | 64GB | 2×2TB ENT HDD  |
| €43/mo | FSN1 | Xeon E3-1275v5 | 64GB | 2×480GB DC SSD |

€43 buys 64 GB, 8× a CPX31's RAM for 2.7× the price: strong for memory-heavy work, only if EU latency is acceptable.

## What would change

Unchanged: Talos machine config generation, secrets and PKI; `talos_machine_configuration_apply` and bootstrap (different IP); Nebula (add a node entry); Cilium, Flux and everything above the OS.

New wiring:

- **Terraform provider**: `hcloud` is Cloud-only. Robot servers need community `hetznercloud/hrobot` (or scripted Robot API calls): a new entry in `terraform.tf`'s providers block.
- **OS install**: Robot has no snapshots (the Cloud flow is Packer: rescue boot → `dd` Talos → snapshot). Instead: Robot API activates rescue → reboot into Debian rescue → `dd` the same Image Factory Talos image → reboot, via `hrobot_boot` and `hrobot_reset` or a `null_resource` provisioner.
- **Firewall**: `hcloud_firewall` does not apply. Use `hrobot_firewall` (other API and rule syntax) or rely on Talos nftables + Nebula; the hcloud firewall mainly covers the initial Talos API exposure window.
- **Node labels**: Talos CCM reads hcloud metadata for topology labels; Robot servers have none, so set labels in the Talos machine config patches.
- **Acquisition**: a one-time manual click in the auction. The server then lands in the Robot account with a fixed ID and static IP, and TF takes over.

Scope: 1–2 new `.tf` files and new node entries; no change to existing infra or node logic. The work is the Robot plumbing (provider, rescue-mode install, firewall), and the community `hrobot` provider is less mature than `hcloud`.

## Open questions

- Which spec/price targets would beat adding Cloud VPS?
- Is `hrobot` maintained well enough, or is scripting safer?
- Rescue-mode install is more stateful than snapshots: how to re-provision after a drive wipe?
- Robot firewall vs. Nebula + Talos nftables?
