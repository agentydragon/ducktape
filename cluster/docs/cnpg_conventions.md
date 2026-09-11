# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Two allowed configurations

Only these CNPG cluster profiles are permitted:

| Profile            | Instances | Pin                                      | Storage                        | Anti-affinity                         |
| ------------------ | --------- | ---------------------------------------- | ------------------------------ | ------------------------------------- |
| **OVH-HA**         | 2         | `topology.kubernetes.io/zone: hil-ovh`   | `local-path-ovh-hdd` or `-ssd` | `topologyKey: kubernetes.io/hostname` |
| **Proxmox-single** | 1         | `topology.kubernetes.io/region: proxmox` | `local-path-proxmox`           | n/a                                   |

**OVH-HA**: For services co-located with the SeaweedFS cluster on the OVH
nodes. Two instances on separate nodes.

**Proxmox-single**: For homelab services. Single instance co-located with the
app on Proxmox. Relies on ZFS for local reliability; off-site backups via
pg_dump CronJobs (see backup strategy in <plan.md>).

**Why only these**: CNPG applies affinity uniformly to all instances (primary
and replicas). If a cluster spans regions, a failover can promote the remote
replica, causing all writes to cross the Nebula mesh indefinitely. Pinning
all instances to one site avoids this.

### R3: CNPG storage class follows the pin

OVH-HA uses `local-path-ovh-hdd` or, for fsync/latency-critical DBs, the
`local-path-ovh-ssd` KS-GAME NVMe tier (`forgejo-db-ssd`,
`seaweedfs-filer-db-ssd`) — both constrained to OVH nodes via
`allowedTopologies`. (`local-path-ovh` is a deprecated alias re-pinned to the
same media as `-hdd`, kept only for already-bound PVCs — see the SC manifest;
new clusters name the tiered classes explicitly.) Proxmox-single uses
`local-path-proxmox`. OVH-HA gets
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

No roster here — it drifts. The SSOT is the `Cluster` manifests themselves;
sweep them with:

```bash
grep -rl 'kind: Cluster' cluster/k8s --include='postgres-cluster*.yaml'
```

As of 2026-08, every **live** (unsuspended) cluster is 2-instance OVH-HA —
most on the deprecated `local-path-ovh` alias, plus `forgejo-db-ssd` and
`seaweedfs-filer-db-ssd` on the `local-path-ovh-ssd` tier — with one deviation:

- `study-casino-db` (`k8s/study-casino/db/`): 3 instances pinned
  `region: hil`, one per OVH node, so it tolerates a node loss without
  quorum or primary impact. Deliberate deviation from the 2-instance
  profile; the manifest comment is the record.

Parked clusters (suspended Flux Kustomizations; R2/R3 bind again on revival):

- `Props` (<../../props/deploy/>): suspended 2026-08-20 for a temporary teardown
  (<decisions.md> § "Suspended Kustomizations").
- Proxmox-single: `firecrawl-db`, `inventree-db`, `tandoor-db` (`x/`). Their
  manifests still name `local-path`, the chart-default StorageClass retired
  2026-06-03 (`k8s/local-path-provisioner/helmrelease.yaml`) — re-point to
  `local-path-proxmox` when reviving.
- `wayback-archive-db` (<../../loom/wayback/deploy/db/>): OVH-HA shape,
  suspended with the rest of wayback-cache.
- `haku-dispatch-db` (`k8s/x/haku/dispatch/db/`): OVH-HA shape; not wired
  into the root `k8s/kustomization.yaml` at all.

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
- [ ] Machine-check R3: validate that every CNPG Cluster uses the storage
      class R3 prescribes for its profile
- [ ] Machine-check R5: validate that app `nodeSelector` region matches its
      CNPG cluster's region
