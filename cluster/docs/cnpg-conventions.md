# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Two allowed configurations

Only two CNPG cluster profiles are permitted:

| Profile            | Instances | Region    | Storage      | Anti-affinity                                          |
| ------------------ | --------- | --------- | ------------ | ------------------------------------------------------ |
| **VPS-HA**         | 2         | `hetzner` | `local-path` | `topologyKey: kubernetes.io/hostname` + CP tolerations |
| **Proxmox-single** | 1         | `proxmox` | `local-path` | n/a                                                    |

**VPS-HA**: For services that must survive without Proxmox (DNS, auth, TF
state). Two instances spread across the two Hetzner VPS nodes. Requires
control-plane tolerations since VPS nodes carry the taint.

**Proxmox-single**: For homelab services. Single instance co-located with the
app on Proxmox. Relies on ZFS for local reliability; off-site backups via
pg_dump CronJobs (see backup strategy in <plan.md>).

**Why only these two**: CNPG applies affinity uniformly to all instances
(primary and replicas). If a cluster spans regions (e.g., primary on Proxmox,
replica on VPS), a failover can promote the remote replica, causing all writes
to cross the Nebula mesh indefinitely. Pinning all instances to one region
avoids this.

### R3: All CNPG clusters use `local-path` storage

No other storage classes. VPS-HA clusters get replication at the CNPG level
(2 instances on separate nodes). Proxmox-single clusters get replication at
the storage level (ZFS on the Proxmox host). Using `longhorn` or
`proxmox-csi-retain` for CNPG would add unnecessary complexity.

### R4: Apps must be pinned to the same region as their DB

An app using a CNPG cluster must have its pods pinned to the same region
(`topology.kubernetes.io/region`) as the database. No floating apps with
pinned DBs or vice versa. This prevents cross-site write latency.

## Current Compliance

| Cluster       | Profile        | Compliant | Notes                                                                      |
| ------------- | -------------- | --------- | -------------------------------------------------------------------------- |
| authentik-db  | VPS-HA         | Yes       |                                                                            |
| powerdns-db   | VPS-HA         | Yes       |                                                                            |
| tofu-state-db | VPS-HA         | Yes       |                                                                            |
| atuin-db      | Proxmox-single | Yes       |                                                                            |
| langfuse-db   | Proxmox-single | Yes       |                                                                            |
| inventree-db  | Proxmox-single | Yes       |                                                                            |
| harbor-db     | Proxmox-single | Yes       |                                                                            |
| gitea-db      | Proxmox-single | Yes       |                                                                            |
| props-db      | Proxmox-single | Yes       |                                                                            |
| matrix-db     | Proxmox-single | Yes       |                                                                            |
| firecrawl-db  | Proxmox-single | Yes       |                                                                            |
| attic-db      | —              | **No**    | 1 instance on Hetzner (documented exception: blocked on containerd 2.2.3+) |

## TODO

- [ ] Fix `attic-db`: upgrade to VPS-HA (2 instances) once the Hetzner pin is
      appropriate, or move to Proxmox-single once containerd blocker resolves
- [ ] Set up off-site backups for Proxmox-single clusters (see "CNPG Backup
      Strategy" in <plan.md>)
- [ ] Deduplicate CNPG cluster configs: extract shared fields (probes,
      monitoring, liveness isolation check) into Kustomize bases or a shared
      patch, so each service only specifies name/namespace/database/size
- [ ] Machine-check R1: pre-commit or CI check that no `image: postgres:*`
      appears in StatefulSets/Deployments outside of CNPG
- [ ] Machine-check R2: validate that every CNPG Cluster matches one of the
      two allowed profiles (instance count, region)
- [ ] Machine-check R3: validate that every CNPG Cluster uses `local-path`
      storage class
- [ ] Machine-check R4: validate that app `nodeSelector` region matches its
      CNPG cluster's region
