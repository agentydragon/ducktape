# Cloud Provider Comparison (May 2026)

Baseline: 4× Hetzner CPX31 @ ~$25/mo each = ~$100/mo total.
CPX31 = 4 vCPU (shared AMD), 8 GB RAM, 160 GB NVMe SSD, Hillsboro OR (HIL).

Note: Hetzner raised CPX31 from ~$18 to ~$25 in April 2026.

## Per-node comparison (~$25 budget, equivalent specs)

| Provider            | Plan         | Type       | vCPU                 | RAM   | Storage       | $/mo    | US West?     | TF provider                                       |
| ------------------- | ------------ | ---------- | -------------------- | ----- | ------------- | ------- | ------------ | ------------------------------------------------- |
| **Hetzner Cloud**   | CPX31        | shared VPS | 4                    | 8 GB  | 160 GB NVMe   | $25     | HIL ✓        | `hcloud` (official, mature)                       |
| **OVHcloud**        | VPS-1        | shared VPS | 4                    | 8 GB  | 75 GB SSD     | $6.46\* | unclear†     | `ovh/ovh` (official, `ovh_vps`)                   |
| **OVHcloud**        | VPS-3        | shared VPS | 8                    | 24 GB | 200 GB NVMe   | $20     | unclear†     | `ovh/ovh` (official, `ovh_vps`)                   |
| **OVH Eco Kimsufi** | KS-1         | dedicated  | 4c/8t Xeon-D 1520    | 32 GB | 2×480 GB SSD  | $20     | HIL ✓        | `ovh/ovh` but **broken for Eco line** (see below) |
| **OVH Eco RISE-S**  | RISE-S       | dedicated  | 8c/16t Ryzen 7 9700X | 64 GB | 2×512 GB NVMe | $77     | HIL ✓        | `ovh/ovh` (official, works for RISE)              |
| **DigitalOcean**    | Basic 4vCPU  | shared VPS | 4                    | 8 GB  | 160 GB SSD    | $48     | SFO3 ✓       | `digitalocean/digitalocean` (official, mature)    |
| **Vultr**           | Cloud HP     | shared VPS | 4                    | 8 GB  | 180 GB SSD    | $48     | LA/SJC ✓     | `vultr/vultr` (official)                          |
| **Linode/Akamai**   | Shared 4vCPU | shared VPS | 4                    | 8 GB  | 160 GB SSD    | $48     | Fremont CA ✓ | `linode/linode` (official)                        |
| **Contabo**         | Cloud VPS 10 | shared VPS | 4                    | 8 GB  | 75 GB NVMe    | ~$7     | Seattle ✓    | none (scripted API only)                          |

\*OVH VPS "starting from" price is likely EU-DC. US location may cost more — unverified.
†OVH VPS US West availability unclear from public docs; dedicated (Eco/RISE) US West confirmed.

## What does ~$100/mo buy?

| Provider                      | Config            | vCPU total     | RAM total  | $/mo  |
| ----------------------------- | ----------------- | -------------- | ---------- | ----- |
| **Hetzner Cloud** _(current)_ | 4× CPX31          | 16 shared      | 32 GB      | ~$100 |
| **OVH Eco Kimsufi**           | 5× KS-1 dedicated | 20c/40t Xeon-D | 160 GB     | $100  |
| **OVH Eco RISE-S**            | 1× Ryzen 7 9700X  | 8c/16t         | 64 GB NVMe | $77   |
| **DigitalOcean SFO3**         | 2× Basic 4vCPU    | 8 shared       | 16 GB      | $96   |
| **Vultr LA/SJC**              | 2× Cloud HP       | 8 shared       | 16 GB      | $96   |
| **Linode/Akamai Fremont**     | 2× Shared 4vCPU   | 8 shared       | 16 GB      | $96   |
| **Contabo Seattle**           | 14× Cloud VPS 10  | 56 shared‡     | 112 GB     | ~$98  |

‡Contabo vCPU count is nominal — see rejection notes below.

## OVH Eco full range (as of May 2026, US West available on all)

| Series       | Plan   | CPU                     | RAM        | Storage         | $/mo |
| ------------ | ------ | ----------------------- | ---------- | --------------- | ---- |
| Kimsufi      | KS-1   | Xeon-D 1520 (4c/8t)     | 32 GB      | 2×480 GB–2×2 TB | $20  |
| Kimsufi      | KS-2   | Xeon-D 1540 (8c/16t)    | 32–64 GB   | 2×450 GB–4×2 TB | $23  |
| Kimsufi      | KS-5   | Xeon-E3 1270 v6 (4c/8t) | 32–64 GB   | 2×450 GB–2×2 TB | $20  |
| Kimsufi      | KS-6   | EPYC 7351p (16c/32t)    | 128–256 GB | 2×500 GB–2×8 TB | $44  |
| So You Start | SYS-3  | Xeon-E 2288G (8c/16t)   | 32–128 GB  | 2×960 GB–2×6 TB | $60  |
| RISE         | RISE-S | Ryzen 7 9700X (8c/16t)  | 64 GB      | 2×512 GB NVMe   | $77  |
| RISE         | RISE-M | Ryzen 9 9900X (12c/24t) | 64 GB      | 2×512 GB NVMe   | $118 |
| RISE         | RISE-L | Ryzen 9 9950X (16c/32t) | 128 GB     | 2×960 GB NVMe   | $177 |

## TF provisioning notes per provider

**Hetzner Cloud** — `hcloud` provider is mature, first-class. Snapshot-based provisioning
(Packer → `hcloud_server`) is already in use. Easiest path.

**OVH VPS** — `ovh/ovh` provider has `ovh_vps` data source (read-only) but no
`ovh_vps` resource for creating VPS instances. VPS creation would require the OVH
Public Cloud (OpenStack) API or manual provisioning.

**OVH Eco RISE** — `ovh/ovh` provider works for RISE/Advance dedicated servers.
Auth requires 3-part OAuth token (app key + app secret + consumer key), not a single
token. Provisioning flow: `ovh_dedicated_server_update` (set rescue boot) →
`ovh_dedicated_server_reboot_task` → `null_resource` remote-exec (dd Talos) →
`talos_machine_configuration_apply`. Server acquisition is manual (order on website
→ server lands in account). Moderate effort.

**OVH Eco Kimsufi** — Same provider as RISE but **broken**: open GitHub issue
[#1176](https://github.com/ovh/terraform-provider-ovh/issues/1176) — the provider
hardcodes the `baremetalServers` endpoint which doesn't work for the Eco product line.
Kimsufi servers would require fully scripted provisioning via `null_resource` +
`local-exec` curl calls to the OVH API, or waiting for the bug to be fixed.

**DigitalOcean / Vultr / Linode** — All have official, mature TF providers. VPS
creation is straightforward (`digitalocean_droplet`, `vultr_instance`,
`linode_instance`). No snapshot needed — pick a base OS image and cloud-init.
However, none of these support bare-metal Talos installs (cloud VPS only), so
you'd run Talos inside a VPS rather than on bare metal, same as Hetzner Cloud.

**Contabo** — No official TF provider. Only option is scripted API calls via
`null_resource` + `local-exec`. Not viable for a clean IaC setup.

## Considered and rejected

### Equinix Metal (bare metal, Silicon Valley / LA)

**Rejected: EOL.** Service ends June 30, 2026. Commercial sales already closed.
Do not investigate further.

### Latitude.sh (bare metal, Los Angeles)

**Rejected: too expensive.** Cheapest plan is $296/mo (6c AMD, 64 GB). Designed
for AI/ML workloads. Complete overkill for general k8s workers.

### Hetzner Robot auction

**Rejected: EU-only.** As of May 2026, all 312 auction servers are in FSN1
(Falkenstein), HEL1 (Helsinki), or NBG1 (Nuremberg). No HIL. Latency from US West
to EU is ~130ms. See `idea/hetzner-auction-k8s-node.md` for full analysis.

### AWS EC2 / GCP / Azure

**Rejected: cost.** A comparable instance (e.g., AWS t3.xlarge: 4 vCPU, 16 GB) runs
~$120–150/mo on-demand in us-west. Reserved instances reduce this but require 1–3yr
commitments. 5–6× the cost of Hetzner for no meaningful benefit in this use case.

### Contabo (US West — Seattle)

**Rejected: quality.** At ~$7/mo for 4vCPU/8GB it looks compelling on paper, but
Contabo is notorious for extreme CPU overselling — the stated vCPU count is nominal
and sustained CPU workloads (e.g. etcd, kube-apiserver) perform like ~1.5–2 real
cores. No official TF provider. Not suitable for k8s control-plane-adjacent nodes.
Might be acceptable for very bursty/idle worker workloads but hard to recommend.

### Linode/Akamai (Fremont CA)

**Not compelling.** $48/mo for 4vCPU/8GB — same price tier as DigitalOcean and
Vultr, 2× Hetzner. Fremont is genuinely closer to California than HIL, but the
value/price ratio doesn't justify switching from Hetzner. Reasonable fallback if
Hetzner HIL has availability issues.

### DigitalOcean (SFO3) / Vultr (LA/SJC)

**Not compelling.** Both $48/mo for 4vCPU/8GB — same specs as Hetzner CPX31 for 2×
the price. Closer to California than HIL but not enough to justify the premium unless
sub-20ms latency to CA is a hard requirement. Good TF provider support. Keep in mind
if Hetzner raises prices again.

## Key takeaways

- **Best value for raw RAM near California**: OVH Eco Kimsufi KS-1 ($20/mo, 32 GB
  dedicated, HIL). 4× the RAM of CPX31 for 20% less. Catch: TF provider broken for
  Eco line (scripted provisioning only), bare metal (no live migration).
- **Best value for a single beefy node**: OVH RISE-S ($77/mo, Ryzen 7 9700X, 64 GB,
  HIL). TF provider works. Rescue-mode Talos install via `null_resource`.
- **Cloud VPS options near California** (DO/Vultr/Linode): all ~$48/mo = 2× Hetzner.
  Only worth it if you need to be in SFO/LA specifically.
- **Hetzner stays best overall** for cloud VPS value at HIL despite April 2026 price
  increase, unless raw RAM density is the priority.
