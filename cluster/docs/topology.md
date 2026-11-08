# Cluster topology and inventories

## Nodes

| Node                                           | Type             | Region    | Availability     | Hardware                         |
| ---------------------------------------------- | ---------------- | --------- | ---------------- | -------------------------------- |
| `ovh-ns104952`, `ovh-ns104963`                 | Talos CP         | `hil`     | Always on        | OVH KS-GAME                      |
| `ovh-ns1001419`                                | Talos CP         | `hil`     | Always on        | OVH SYS-1, 64 GiB, 2x512 GB NVMe |
| `ovh-ns103656`, `ovh-ns103711`, `ovh-ns102453` | Talos worker     | `hil`     | Always on        | OVH Kimsufi KS-5                 |
| `optiplex`                                     | Talos worker     | `home`    | Always on (home) | Dell OptiPlex 7060               |
| `wyrm2`                                        | NixOS GPU worker | `proxmox` | Always on (home) | 2x RTX 5090                      |
| `iguana`                                       | NixOS laptop     | `roaming` | Often offline    | ThinkPad X1 Extreme              |
| `rugged`                                       | NixOS laptop     | `roaming` | Often offline    | Dell Rugged 12                   |

## Services

| Service        | URL                               | Purpose                 |
| -------------- | --------------------------------- | ----------------------- |
| Authentik      | <https://auth.allegedly.works>    | SSO                     |
| Forgejo        | <https://git.allegedly.works>     | Git hosting             |
| Matrix/Element | <https://chat.allegedly.works>    | Chat                    |
| Grafana        | <https://grafana.allegedly.works> | Monitoring              |
| Nix Cache      | <https://cache.allegedly.works>   | Binary cache            |
| Gatus          | <https://status.allegedly.works>  | Health monitoring       |
| Ollama         | <https://ollama.allegedly.works>  | LLM inference (GPU)     |
| Airlock        | <https://airlock.allegedly.works> | OAuth credential broker |

## Storage

| StorageClass                | Provisioner       | Placement and use                             |
| --------------------------- | ----------------- | --------------------------------------------- |
| `seaweedfs-ovh-ssd`         | SeaweedFS CSI     | OVH SSD SeaweedFS; Forgejo git hot tier       |
| `piraeus-ssd-local`         | Piraeus (LINSTOR) | One local SSD copy on a node with an SSD pool |
| `piraeus-hdd-local`         | Piraeus (LINSTOR) | One local HDD copy on a node with an HDD pool |
| `piraeus-ssd-2-autodiskful` | Piraeus (LINSTOR) | Two OVH SSD copies with autodiskful placement |
| `piraeus-hdd-2`             | Piraeus (LINSTOR) | Two OVH HDD copies with autodiskful placement |

See <piraeus.md> and <../cdk8s/piraeus.py> for storage pools, placement and
recovery constraints. Existing PVCs can retain older classes during migrations.

## Failure modes

| Scenario              | Cluster    | Ingress/DNS/Authentik | Notes                                             |
| --------------------- | ---------- | --------------------- | ------------------------------------------------- |
| Single OVH CP down    | 2/3 quorum | Work                  | Surviving OVH CP carries ingress + lighthouse     |
| Multiple OVH CPs down | 1/3        | Down                  | Home pods continue; cluster frozen                |
| Home down             | 3/3 quorum | Work                  | Public-critical services run on OVH-local storage |
