# Raw PVC Inventory

PVCs not managed by CNPG or a Valkey/Redis operator. `Active` means at least
one live pod mounted the PVC during the 2026-05-20 inventory refresh.

Last updated: 2026-05-20

| PVC                                        | Size   | StorageClass           | Status | What it holds                                             |
| ------------------------------------------ | ------ | ---------------------- | ------ | --------------------------------------------------------- |
| `arc-runners/cache-github-runner-0`        | 50Gi   | local-path             | Active | GitHub Actions runner cache                               |
| `cpap-sync/cpap-data`                      | 50Gi   | lvm-proxmox-hdd-shared | Active | CPAP sync data                                            |
| `gatus/gatus`                              | 200Mi  | local-path             | Active | Gatus status check DB (SQLite)                            |
| `grocy-sf/grocy-config-ovh`                | 1Gi    | local-path-ovh         | Active | Grocy SF app data (SQLite + uploads)                      |
| `grocy-vallejo/grocy-config-ovh`           | 1Gi    | local-path-ovh         | Active | Grocy Vallejo app data (SQLite + uploads)                 |
| `harbor/harbor-jobservice`                 | 1Gi    | lvm-proxmox-hdd        | Active | Harbor job logs                                           |
| `harbor/harbor-registry`                   | 30Gi   | lvm-proxmox-hdd        | Active | Container image blobs                                     |
| `loki/storage-loki-0`                      | 10Gi   | local-path-hetzner     | Active | Loki local WAL/cache; long-term data is in object storage |
| `matrix/matrix-synapse`                    | 20Gi   | local-path-proxmox     | Active | Synapse media and state                                   |
| `monitoring/db-alertmanager-monitoring-0`  | 1Gi    | local-path-hetzner     | Active | Alertmanager notification and silence state               |
| `monitoring/db-alertmanager-monitoring-1`  | 1Gi    | local-path-hetzner     | Active | Alertmanager notification and silence state               |
| `monitoring/storage-mimir-compactor-0`     | 10Gi   | local-path-hetzner     | Active | Mimir compactor local working state/cache                 |
| `monitoring/storage-mimir-ingester-0`      | 10Gi   | local-path-hetzner     | Active | Mimir ingester TSDB/WAL                                   |
| `monitoring/storage-mimir-store-gateway-0` | 10Gi   | local-path-hetzner     | Active | Mimir store-gateway cache/local state                     |
| `ollama/llm-models`                        | 200Gi  | lvm-proxmox-hdd        | Active | LLM model weights                                         |
| `openhands/openhands-data`                 | 10Gi   | local-path-proxmox     | Active | OpenHands workspace data                                  |
| `seaweedfs/mount0-seaweedfs-volume-0`      | 1800Gi | local-path-ovh         | Active | SeaweedFS volume server data                              |
| `seaweedfs/mount0-seaweedfs-volume-1`      | 1800Gi | local-path-ovh         | Active | SeaweedFS volume server data                              |
| `tana-mcp/tana-mcp-config`                 | 10Gi   | hcloud-volumes         | Active | Tana MCP state                                            |
| `thrive-scraper/thrive-data`               | 10Gi   | lvm-proxmox-hdd-shared | Active | Thrive scraper data                                       |
| `tofu-state/tofu-state-backup`             | 1Gi    | local-path-proxmox     | Active | tofu state backups                                        |

## Hetzner Local-Path PVCs

These PVCs still have `local-path-hetzner` PVs:

| PVC                                        | Node               | Status | Note                                   |
| ------------------------------------------ | ------------------ | ------ | -------------------------------------- |
| `loki/storage-loki-0`                      | talos-vps-worker-0 | Active | Mounted by `loki-0`                    |
| `monitoring/db-alertmanager-monitoring-0`  | talos-vps-cp-0     | Active | Mounted by `alertmanager-monitoring-0` |
| `monitoring/db-alertmanager-monitoring-1`  | talos-vps-cp-1     | Active | Mounted by `alertmanager-monitoring-1` |
| `monitoring/grafana-db-1`                  | talos-vps-cp-1     | Active | CNPG-managed Grafana Postgres data     |
| `monitoring/grafana-db-2`                  | talos-vps-cp-0     | Active | CNPG-managed Grafana Postgres data     |
| `monitoring/storage-mimir-compactor-0`     | talos-vps-worker-0 | Active | Mounted by `mimir-compactor-0`         |
| `monitoring/storage-mimir-ingester-0`      | talos-vps-worker-0 | Active | Mounted by `mimir-ingester-0`          |
| `monitoring/storage-mimir-store-gateway-0` | talos-vps-worker-0 | Active | Mounted by `mimir-store-gateway-0`     |
