# Cloud provider comparison (May 2026)

Baseline when written: 4× Hetzner CPX31 (4 shared AMD vCPU, 8 GB, 160 GB NVMe, Hillsboro `HIL`) at ~$25/mo each, ~$100 total; Hetzner raised CPX31 from ~$18 in April 2026. The cluster has since moved to OVH dedicated servers in `hil` ([nodes](../cluster/docs/topology.md#nodes)). To refresh prices: `cluster/scripts/ovh_hil_availability.py` (sub-$100 OVH dedicated in Hillsboro) and `cluster/scripts/hcloud_server_types.py` (Hetzner types).

## Per-node comparison (~$25 budget, equivalent specs)

| Provider            | Plan         | Type       | vCPU                 | RAM   | Storage       | $/mo    | US West?     |
| ------------------- | ------------ | ---------- | -------------------- | ----- | ------------- | ------- | ------------ |
| **Hetzner Cloud**   | CPX31        | shared VPS | 4                    | 8 GB  | 160 GB NVMe   | $25     | HIL ✓        |
| **OVHcloud**        | VPS-1        | shared VPS | 4                    | 8 GB  | 75 GB SSD     | $6.46\* | unclear†     |
| **OVHcloud**        | VPS-3        | shared VPS | 8                    | 24 GB | 200 GB NVMe   | $20     | unclear†     |
| **OVH Eco Kimsufi** | KS-1         | dedicated  | 4c/8t Xeon-D 1520    | 32 GB | 2×480 GB SSD  | $20     | HIL ✓        |
| **OVH Eco RISE-S**  | RISE-S       | dedicated  | 8c/16t Ryzen 7 9700X | 64 GB | 2×512 GB NVMe | $77     | HIL ✓        |
| **DigitalOcean**    | Basic 4vCPU  | shared VPS | 4                    | 8 GB  | 160 GB SSD    | $48     | SFO3 ✓       |
| **Vultr**           | Cloud HP     | shared VPS | 4                    | 8 GB  | 180 GB SSD    | $48     | LA/SJC ✓     |
| **Linode/Akamai**   | Shared 4vCPU | shared VPS | 4                    | 8 GB  | 160 GB SSD    | $48     | Fremont CA ✓ |
| **Contabo**         | Cloud VPS 10 | shared VPS | 4                    | 8 GB  | 75 GB NVMe    | ~$7     | Seattle ✓    |

\*OVH VPS "starting from" price is likely an EU DC; the US price may be higher (unverified). †OVH VPS US West availability is unclear from public docs; dedicated (Eco/RISE) US West is confirmed.

What ~$100/mo buys: 4× CPX31 = 16 shared vCPU, 32 GB; 5× KS-1 = 20c/40t, 160 GB; 1× RISE-S = 8c/16t, 64 GB NVMe ($77); 2× DO/Vultr/Linode = 8 shared vCPU, 16 GB ($96); 14× Contabo VPS 10 = 56 nominal vCPU, 112 GB (~$98).

## OVH Eco full range (May 2026, US West available on all)

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

## Terraform provisioning per provider

- **Hetzner Cloud**: `hcloud` is mature; Packer snapshot → `hcloud_server` was in use. Easiest.
- **OVH VPS**: `ovh/ovh` has an `ovh_vps` data source only, no resource. Creating VPS needs the Public Cloud (OpenStack) API or manual provisioning.
- **OVH Eco RISE**: `ovh/ovh` works. Auth is a 3-part OAuth token (app key, app secret, consumer key). Flow: `ovh_dedicated_server_update` (rescue boot) → `ovh_dedicated_server_reboot_task` → `null_resource` remote-exec (`dd` Talos) → `talos_machine_configuration_apply`. Ordering the server is manual (website order → lands in the account).
- **OVH Eco Kimsufi**: same provider, **broken** for Eco: [ovh/terraform-provider-ovh#1176](https://github.com/ovh/terraform-provider-ovh/issues/1176), the provider hardcodes the `baremetalServers` endpoint. Needs scripted `null_resource` + `local-exec` curl against the OVH API, or the fix.
- **DigitalOcean / Vultr / Linode**: official mature providers (`digitalocean_droplet`, `vultr_instance`, `linode_instance`); base image + cloud-init, no snapshot. Cloud VPS only: Talos runs inside a VPS, not on bare metal, as on Hetzner Cloud.
- **Contabo**: no official provider; only scripted API calls via `null_resource` + `local-exec`. Not viable for clean IaC.

## Considered and rejected

- **Equinix Metal** (Silicon Valley / LA bare metal): EOL, service ends June 30, 2026, sales closed.
- **Latitude.sh** (Los Angeles bare metal): cheapest plan $296/mo (6c AMD, 64 GB), built for AI/ML; overkill for k8s workers.
- **Hetzner Robot auction**: EU-only. All 312 auction servers are in FSN1, HEL1 or NBG1 (May 2026), no HIL; ~130 ms from US West. Analysis: `idea/hetzner_auction_k8s_node.md`.
- **AWS / GCP / Azure**: cost. t3.xlarge (4 vCPU, 16 GB) is ~$120–150/mo on-demand in us-west; reserved needs 1–3 yr commitments. 5–6× Hetzner for no benefit here.
- **Contabo Seattle**: quality. ~$7/mo for 4 vCPU/8 GB, but heavy CPU oversell: the stated vCPU count is nominal, and sustained loads (etcd, kube-apiserver) get ~1.5–2 real cores. No TF provider. Unfit for control-plane-adjacent nodes; maybe bursty or idle workers.
- **Linode/Akamai Fremont, DigitalOcean SFO3, Vultr LA/SJC**: not compelling. $48/mo for 4 vCPU/8 GB is 2× CPX31. Closer to California than HIL, but only worth it if sub-20 ms to CA is a hard requirement. Fallback if HIL availability suffers or Hetzner raises prices again.

## Key takeaways

- Best raw RAM near California: OVH Eco Kimsufi KS-1 ($20/mo, 32 GB dedicated, HIL): 4× the RAM of CPX31 for 20% less. Catch: TF provider broken for Eco (scripted provisioning), bare metal (no live migration).
- Best single beefy node: OVH RISE-S ($77/mo, Ryzen 7 9700X, 64 GB, HIL). TF provider works; rescue-mode Talos install via `null_resource`.
- DO/Vultr/Linode near California: ~$48/mo, 2× Hetzner; only for needing SFO/LA specifically.
- Hetzner was the best cloud VPS value at HIL despite the April 2026 increase, unless RAM density matters.
