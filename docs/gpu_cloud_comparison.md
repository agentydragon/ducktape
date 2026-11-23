# GPU Cloud Comparison: A100/H100-tier (May 2026)

Scope: renting 1–8× A100 or H100-class GPUs for compute workloads.
Not covering consumer-tier (RTX 4090 etc.) or older (V100, T4) GPUs.

## PCIe vs SXM matters for multi-GPU

- **PCIe**: cheaper, GPU connected via PCIe bus. Fine for inference, single-GPU
  training, or loosely-coupled multi-GPU via NVLink bridges.
- **SXM**: on a baseboard with NVLink, significantly higher GPU-to-GPU bandwidth
  (~600 GB/s vs ~64 GB/s). Required for tightly-coupled multi-GPU training (DDP,
  tensor parallelism). SXM instances are almost always sold as full 8-GPU nodes.

For inference or single-GPU training: PCIe is fine and cheaper.
For serious multi-GPU training: you want SXM 8× nodes.

## H100 — price per GPU (May 2026, 24/7 = 730 hrs/mo)

| Provider               | $/GPU/hr | $/GPU/mo | Type      | Min unit     | Notes                                                |
| ---------------------- | -------- | -------- | --------- | ------------ | ---------------------------------------------------- |
| **Vast.ai**            | $1.23    | ~$900    | spot      | 1×           | Variable, interruptible. Can be much lower off-peak. |
| **Thunder Compute**    | $1.38    | ~$1,010  | on-demand | 1×           | Smaller provider, NVL variant                        |
| **Hyperstack**         | $1.90    | ~$1,390  | on-demand | 1×           | —                                                    |
| **RunPod** (community) | $2.69    | ~$1,965  | on-demand | 1×           | SXM; community hardware, less SLA                    |
| **RunPod** (secure)    | $2.99    | ~$2,185  | on-demand | 1×           | SXM; dedicated hosts, better reliability             |
| **OVHcloud** (Public)  | ~$3.00   | ~$2,190  | on-demand | 1×           | EU/CA primary; US region availability unclear; PCIe  |
| **Vultr**              | $2.99    | ~$17,500 | on-demand | 8× only      | Per-node (8 GPUs); multi-GPU only                    |
| **Lambda Labs**        | $3.29    | ~$2,400  | on-demand | 1×           | PCIe; most reliable availability of mid-tier         |
| **DigitalOcean**       | $3.39    | ~$2,475  | on-demand | 1×           | —                                                    |
| **CoreWeave**          | $6.16    | ~$36,000 | on-demand | 8× (SXM HGX) | Per-node; enterprise; k8s-native; contract pricing   |
| **AWS p5.4xlarge**     | $6.88    | ~$5,025  | on-demand | 1×           | SXM; p5.48xlarge (8×) = ~$71,800/mo                  |
| **Azure NCadsH100v5**  | $6.98    | ~$5,095  | on-demand | 1×           | —                                                    |

## A100 — price per GPU (May 2026, 24/7 = 730 hrs/mo)

| Provider               | $/GPU/hr   | $/GPU/mo  | Type      | Min unit | Notes                                        |
| ---------------------- | ---------- | --------- | --------- | -------- | -------------------------------------------- |
| **Vast.ai**            | $0.14–0.76 | ~$100–555 | spot      | 1×       | Wildly variable; avg spot ~$0.76             |
| **Thunder Compute**    | $0.78      | ~$570     | on-demand | 1×       | 80GB                                         |
| **RunPod** (community) | $1.19      | ~$870     | on-demand | 1×       | —                                            |
| **Hyperstack**         | $1.35      | ~$985     | on-demand | 1×       | 80GB                                         |
| **RunPod** (secure)    | $1.39      | ~$1,015   | on-demand | 1×       | —                                            |
| **Lambda Labs**        | $1.99      | ~$1,455   | on-demand | 1×       | 40GB variant                                 |
| **CoreWeave**          | $2.21      | ~$1,615   | on-demand | 1×       | 80GB; enterprise                             |
| **AWS p4d.24xlarge**   | $4.10      | ~$23,960  | on-demand | 8× only  | Per-node (8× A100 40GB); $32.77/hr full node |

## Monthly cost — 8× H100 SXM node

| Provider        | $/mo approx     |
| --------------- | --------------- |
| RunPod secure   | ~$17,200        |
| Lambda Labs     | ~$17,000–19,000 |
| CoreWeave       | ~$32,000+       |
| AWS p5.48xlarge | ~$71,800        |

## Provider profiles

- **Vast.ai**: GPU marketplace (individual and datacenter sellers). Cheapest option
  by far for spot/interruptible work. Hardware quality depends on the host and uptime
  is not guaranteed: good for batch jobs that checkpoint and resume, bad for anything
  needing continuous uptime or an SLA. Not suitable as a permanent k8s node.
- **RunPod**: **Community Cloud** (someone's hardware, cheaper, less reliable) and
  **Secure Cloud** (dedicated datacenter hardware, more reliable). Good mid-tier option.
  Pods are short-lived instances: joining one to a k8s cluster as a persistent worker is
  possible but non-trivial (node churn on interruption). Better for on-demand batch
  work than as a static cluster node.
- **Lambda Labs**: most established mid-tier GPU cloud. Reliable on-demand availability,
  clean API, mid-range pricing: production-grade GPU workloads without the hyperscaler
  premium. You get a VM; no k8s integration out of the box.
- **CoreWeave**: **K8s-native GPU cloud.** You get actual Kubernetes clusters with GPU
  node pools (NVIDIA device plugin, GPU operator, etc. pre-configured). Directly
  relevant for extending our cluster or running GPU workloads on k8s. More expensive
  than RunPod/Lambda (~$6/GPU/hr H100), with enterprise SLAs and contract pricing;
  sales-driven for larger commitments. Probably the best fit for GPU nodes in a k8s
  context without DIY plumbing.
- **AWS / GCP / Azure**: **rejected on cost.** 2–5× more per GPU/hr than mid-tier
  providers for comparable hardware. AWS p5.48xlarge (8× H100 SXM) is $98/hr on-demand,
  $71k/mo. Worth it only if deeply integrated with their ecosystem (managed storage,
  networking, IAM, SageMaker, Vertex AI, etc.) or if you need enterprise SLAs without
  vendor risk. For raw compute they are a bad deal.
- **Hetzner (GPU)**: **not H100/A100.** Dedicated servers with NVIDIA RTX (4000 Ada,
  6000 Ada, RTX PRO 6000 Blackwell), one GPU per server, EU-only (FSN/NBG/HEL), from
  ~€184/mo. Good value for RTX-class inference; not relevant for serious training.
- **Rejected**: Equinix Metal (EOL June 30, 2026); Latitude.sh (H100 offerings exist but
  at $2,000+/mo for the smallest config; AI/ML focused but priced for enterprise);
  Paperspace ($5.99/GPU/hr H100: expensive relative to Lambda/RunPod for no clear
  benefit; acquired by DigitalOcean, future unclear).

### OVHcloud (Bare Metal + Public Cloud)

**West US (HIL — Hillsboro, OR) availability is poor.** OVH has two US datacenters:
HIL (west, where our cluster lives) and VIN (Vint Hill, VA, east; ~70ms RTT from HIL).
As of May 2026:

- **No GPU bare metal is orderable in HIL.** The only GPU SKU available in any US DC is
  HGR-AI-1 in **VIN only** (V100s, $4,446/mo + $3,299 setup).
- **HGR-AI-2 (L40S) and SCALE-GPU-\* (L4)** show "Soon available" globally — no firm
  HIL date.
- **Public Cloud GPU VMs** (L4, L40S, V100S, H100) are sold globally, but OVH's US
  Public Cloud pricing page doesn't break out which DC has stock; historically
  H100/H200 launches lag EU/Canada by months.
- **No H100 SXM or PCIe in OVH's bare metal catalog.** The $2.99/GPU/hr figure in our
  prior entry referred to OVH's Public Cloud H100 VMs, not bare metal. L40S (HGR-AI-2) is
  the closest bare-metal substitute — strong FP8/INT8 inference performance, 48GB VRAM,
  no NVLink. Not a real H100 alternative for >30B training.

**Bare metal GPU lineup (May 2026):**

| SKU         | GPU                                                              | CPU / RAM / Storage                                            | $/mo USD    | $/GPU/mo | West US (HIL) | East US (VIN)      | Notes                            |
| ----------- | ---------------------------------------------------------------- | -------------------------------------------------------------- | ----------- | -------- | ------------- | ------------------ | -------------------------------- |
| HGR-AI-1    | 4× V100S 32GB                                                    | 2× Xeon Gold 6226R, 1.5TB DDR4, 15.94TB SSD                    | $4,446      | $1,112   | ❌            | ✅ (+$3,299 setup) | Old V100; PCIe                   |
| HGR-AI-2    | NVIDIA L40S (PCIe; count not in catalog, sold as multi-GPU node) | 2× EPYC 9354 (64c), 384GB–2.25TB, 2× to 4× NVMe, 100 Gbps priv | from $3,505 | —        | ❌ ("soon")   | ❌ ("soon")        | L40S is H100-class FP8 inference |
| SCALE-GPU-1 | 2× L4 24GB                                                       | EPYC Genoa 32c, 192GB+, NVMe, 50 Gbps priv                     | from $1,145 | $573     | ❌ ("soon")   | ❌ ("soon")        | Entry tier, inference/VDI        |
| SCALE-GPU-2 | L4                                                               | EPYC Genoa, larger config                                      | from $1,180 | —        | ❌ ("soon")   | ❌ ("soon")        | —                                |
| SCALE-GPU-3 | L4                                                               | EPYC Genoa, larger config                                      | from $1,216 | —        | ❌ ("soon")   | ❌ ("soon")        | —                                |

**Public Cloud GPU (VM instances)** — confirmed US-available pricing:

| Flavor   | GPU           | vCPU/RAM    | $/hr  | $/GPU/mo |
| -------- | ------------- | ----------- | ----- | -------- |
| t2-45    | 1× V100S 32GB | 15c / 45GB  | $0.88 | ~$642    |
| l4-90    | 1× L4 24GB    | 22c / 90GB  | $1.00 | ~$730    |
| l40s-90  | 1× L40S 48GB  | 15c / 90GB  | $1.80 | ~$1,314  |
| l40s-360 | 4× L40S       | 60c / 360GB | $7.20 | ~$1,314  |

OVH's published H100 Public Cloud rate is €2.80/hr (~$3.00/hr) for 1× H100 80GB PCIe —
comparable to RunPod Secure and cheaper than Lambda Labs.

**Terraform:** `ovh/ovh` provider supports `ovh_dedicated_server`,
`ovh_dedicated_server_update`, `ovh_dedicated_server_boots`, `ovh_me_ipxe_script` —
same surface we use for Kimsufi. Public Cloud GPU instances provision via
`ovh_cloud_project_instance`. Ordering bare-metal HGR-AI via API has historically
required a manual quote step; reorders are scriptable.

**Verdict for our use case (west US / HIL): pass for now.** A GPU node co-located with
the rest of the cluster (Talos VPS workers + Kimsufi worker; Nebula latency, cross-AZ
bandwidth) has to be in HIL, and OVH has zero orderable GPU SKUs there. The only US
bare-metal GPU is HGR-AI-1 in VIN (V100-generation hardware), HGR-AI-2 (L40S) hasn't
shipped anywhere yet, and Public Cloud H100 at ~$3/hr might land in HIL eventually but
has no current confirmation. Keep wyrm2 / Lambda Labs / RunPod as the GPU paths until
OVH ships an HGR-AI in HIL, and verify on `pricelist.ovh` before assuming HGR-AI-2 /
SCALE-GPU-\* are bookable there.

## TF / provisioning

| Provider        | TF provider                           | Provisioning model                   |
| --------------- | ------------------------------------- | ------------------------------------ |
| **Lambda Labs** | `lambdal/lambdacloud` (official)      | VM per instance via API, SSH in      |
| **RunPod**      | `RunPod/runpod` (official)            | Pod API; less suited to Talos        |
| **Vast.ai**     | `vast-ai/vastai` (community)          | Marketplace API; unreliable for IaC  |
| **CoreWeave**   | Native k8s API                        | You get a kubeconfig; GPU node pools |
| **AWS**         | `hashicorp/aws` (official, mature)    | EC2 instances                        |
| **GCP**         | `hashicorp/google` (official, mature) | GCE instances                        |
| **Hetzner**     | `hetznercloud/hcloud` (official)      | Dedicated root server                |

## K8s integration notes

Joining a GPU node to the existing Talos cluster requires:

1. Talos installed on the node (needs rescue-mode install for bare metal, or a
   GPU-capable Talos image with NVIDIA extensions from Image Factory)
2. Nebula cert for the node (same as any other node)
3. NVIDIA device plugin deployed to the cluster (via Helm/Flux)
4. The GPU provider must allow arbitrary OS installation — most VM providers do,
   bare-metal providers (Hetzner dedicated, OVH, CoreWeave) definitely do

- **CoreWeave** is the odd one out: their k8s clusters are managed by them, not
  Talos-based. Run workloads there independently or federate with the existing cluster
  (more complex); probably better treated as a separate GPU cluster.
- **Lambda Labs / RunPod VMs** can run Talos and join via Nebula in principle, but
  instance churn (interruptions, billing stops) creates node lifecycle issues for k8s.
  Better to treat them as standalone compute rather than persistent cluster nodes.
- **Dedicated bare metal with GPU** (e.g. OVH with GPU option, Hetzner dedicated GPU,
  HOSTKEY) is the cleanest path for a persistent k8s GPU worker node — install Talos,
  join Nebula, join cluster, stays up indefinitely.
