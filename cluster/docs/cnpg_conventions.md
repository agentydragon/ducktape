# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Three allowed configurations

Only these CNPG cluster profiles are permitted:

| Profile            | Instances | Pin                                      | Storage          | Anti-affinity                                          |
| ------------------ | --------- | ---------------------------------------- | ---------------- | ------------------------------------------------------ |
| **VPS-HA**         | 2         | `topology.kubernetes.io/region: hil`     | `local-path`     | `topologyKey: kubernetes.io/hostname` + CP tolerations |
| **OVH-HA**         | 2         | `topology.kubernetes.io/zone: hil-ovh`   | `local-path-ovh` | `topologyKey: kubernetes.io/hostname`                  |
| **Proxmox-single** | 1         | `topology.kubernetes.io/region: proxmox` | `local-path`     | n/a                                                    |

**VPS-HA**: For services that must survive without Proxmox (DNS, auth, TF
state). Two instances spread across the two Hetzner VPS nodes. Requires
control-plane tolerations since VPS nodes carry the taint.

**OVH-HA**: For services co-located with the SeaweedFS cluster on the two OVH
kimsufi workers. Two instances on separate kimsufi nodes (same `hil` region
as VPS-HA but the `hil-ovh` zone). No CP tolerations needed.

**Proxmox-single**: For homelab services. Single instance co-located with the
app on Proxmox. Relies on ZFS for local reliability; off-site backups via
pg_dump CronJobs (see backup strategy in <plan.md>).

**Why only these**: CNPG applies affinity uniformly to all instances (primary
and replicas). If a cluster spans regions, a failover can promote the remote
replica, causing all writes to cross the Nebula mesh indefinitely. Pinning
all instances to one site avoids this.

### R3: CNPG storage class follows the pin

VPS-HA and Proxmox-single use `local-path`; OVH-HA uses `local-path-ovh`
(constrained to the OVH kimsufi nodes via `allowedTopologies`). HA profiles
get replication at the CNPG level (2 instances on separate nodes);
Proxmox-single gets replication at the storage level (ZFS on the Proxmox
host). No `longhorn` or `proxmox-csi-retain` for CNPG.

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

| Cluster       | Profile        | Compliant | Notes |
| ------------- | -------------- | --------- | ----- |
| airlock-db    | VPS-HA         | Yes       |       |
| authentik-db  | VPS-HA         | Yes       |       |
| powerdns-db   | VPS-HA         | Yes       |       |
| tofu-state-db | VPS-HA         | Yes       |       |
| atuin-db      | Proxmox-single | Yes       |       |
| langfuse-db   | Proxmox-single | Yes       |       |
| inventree-db  | Proxmox-single | Yes       |       |
| harbor-db     | Proxmox-single | Yes       |       |
| gitea-db      | Proxmox-single | Yes       |       |
| props-db      | Proxmox-single | Yes       |       |
| matrix-db     | Proxmox-single | Yes       |       |
| tandoor-db    | Proxmox-single | Yes       |       |
| attic-db      | OVH-HA         | Yes       |       |

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
