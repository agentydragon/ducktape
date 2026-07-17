# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Two allowed configurations

Only these CNPG cluster profiles are permitted:

| Profile            | Instances | Pin                                      | Storage          | Anti-affinity                         |
| ------------------ | --------- | ---------------------------------------- | ---------------- | ------------------------------------- |
| **OVH-HA**         | 2         | `topology.kubernetes.io/zone: hil-ovh`   | `local-path-ovh` | `topologyKey: kubernetes.io/hostname` |
| **Proxmox-single** | 1         | `topology.kubernetes.io/region: proxmox` | `local-path`     | n/a                                   |

**OVH-HA**: For services co-located with the SeaweedFS cluster on the two OVH
kimsufi workers. Two instances on separate kimsufi nodes.

**Proxmox-single**: For homelab services. Single instance co-located with the
app on Proxmox. Relies on ZFS for local reliability; off-site backups via
pg_dump CronJobs (see backup strategy in <plan.md>).

**Why only these**: CNPG applies affinity uniformly to all instances (primary
and replicas). If a cluster spans regions, a failover can promote the remote
replica, causing all writes to cross the Nebula mesh indefinitely. Pinning
all instances to one site avoids this.

### R3: CNPG storage class follows the pin

OVH-HA uses `local-path-ovh` (constrained to the OVH kimsufi nodes via
`allowedTopologies`); Proxmox-single uses `local-path`. OVH-HA gets
replication at the CNPG level (2 instances on separate nodes);
Proxmox-single gets replication at the storage level (ZFS on the Proxmox
host).

### R4: `initdb.secret` must exist before the Cluster resource

CNPG reads `bootstrap.initdb.secret` only at cluster creation time to set
the database owner password. If the Secret doesn't exist yet, CNPG
auto-generates a random password and ignores the Secret when it arrives
later. This makes the SOPS-managed password useless.

In the kustomize `resources` list, always list the credentials Secret
**before** the Cluster manifest. Kustomize applies in list order.

### R5: Apps must be pinned to the same region as their DB

An app using a CNPG cluster must have its pods pinned to the same region
(`topology.kubernetes.io/region`) as the database. No floating apps with
pinned DBs or vice versa. This prevents cross-site write latency.

## Current Compliance

| Cluster                | Profile                       | Compliant   |
| ---------------------- | ----------------------------- | ----------- |
| authentik-db-ovh       | OVH-HA                        | Yes         |
| gatus-db               | OVH-HA                        | Yes         |
| grafana-db-ovh         | OVH-HA                        | Yes         |
| tofu-state-db-ovh      | OVH-HA                        | Yes         |
| attic-db               | OVH-HA                        | Yes         |
| seaweedfs-filer-db-ssd | OVH-HA                        | Yes         |
| atuin-db               | OVH-HA                        | Yes         |
| forgejo-db             | OVH-HA                        | Yes         |
| langfuse-db            | OVH-HA                        | Yes         |
| inventree-db           | Proxmox-single                | Yes         |
| harbor-db              | OVH single-instance (interim) | Exception\* |
| props-db               | OVH-HA                        | Yes         |
| matrix-db              | Proxmox-single                | Yes         |
| tandoor-db             | Proxmox-single                | Yes         |

\* `harbor-db` moved off Proxmox 2026-06-03 (`region: hil` / `local-path-ovh`) so
Harbor's DB survives Proxmox downtime, but it's still a single instance rather than
the 2-instance `zone: hil-ovh` OVH-HA shape — see the comment in
`k8s/harbor/db/postgres-cluster.yaml`. Bumping it to a proper OVH-HA cluster (R2/R3)
is a known, deliberately deferred follow-up, not an oversight.

## TODO

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
