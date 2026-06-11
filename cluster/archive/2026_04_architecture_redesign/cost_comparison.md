# Provider Cost Comparison

## Current VPS Sizing

| Role        | Type  | Cores    | RAM  | Disk   | USD/mo    |
| ----------- | ----- | -------- | ---- | ------ | --------- |
| CP (x2)     | CPX31 | 4 shared | 8 GB | 160 GB | 24.99     |
| Worker (x2) | CPX31 | 4 shared | 8 GB | 160 GB | 24.99     |
| **Total**   |       | 16       | 32GB | 640 GB | **99.96** |

Planned upgrade to CPX41: 8 vCPU / 16 GB RAM / 240 GB per node → ~€67/mo total (~$73).

Plus home infrastructure (free aside from electricity):

- atlas (Proxmox): lightweight control plane VM (~4 GB RAM)
- wyrm2 (Proxmox GPU worker): ~28 GB RAM, 2x RTX 5090

## Hetzner vs Linode

|         | Linode ($29/mo) | Hetzner 2x CPX31 (~$33/mo) |
| ------- | --------------- | -------------------------- |
| CPU     | 2 cores         | 8 vCPU (4×2)               |
| RAM     | 4 GB            | 16 GB (8×2)                |
| Storage | 80 GB           | 320 GB (160×2)             |

4× the CPU, RAM, and storage for roughly the same price. Even after the CPX41 upgrade (~$73/mo), 16 vCPU / 32 GB RAM / 480 GB storage still beats Linode per-dollar.

## Hetzner HIL Availability (checked 2026-04-01)

CPX31/41/51 no longer available at HIL for new provisioning. Existing
nodes are grandfathered. In-place resize via `hcloud` provider is
possible (`keep_disk = true`, brief downtime per node).

| Server    | vCPU | RAM   | Type            | EUR/mo | Available |
| --------- | ---- | ----- | --------------- | ------ | --------- |
| CPX11     | 2    | 2 GB  | Shared (AMD)    | 4.49   | Yes       |
| CPX21     | 3    | 4 GB  | Shared (AMD)    | 8.99   | Yes       |
| CPX31     | 4    | 8 GB  | Shared (AMD)    | 15.99  | **No**    |
| **CCX13** | 2    | 8 GB  | Dedicated (AMD) | 12.99  | Yes       |
| CCX23     | 4    | 16 GB | Dedicated (AMD) | 25.99  | Yes       |
