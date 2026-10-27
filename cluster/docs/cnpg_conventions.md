# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Allowed configurations

Only these CNPG cluster profiles are permitted:

| Profile             | Instances | Pin                                      | Storage                                            | Anti-affinity                      |
| ------------------- | --------- | ---------------------------------------- | -------------------------------------------------- | ---------------------------------- |
| **OVH-HA**          | 2         | `topology.kubernetes.io/zone: hil-ovh`   | HDD: `piraeus-hdd-local`; SSD: `piraeus-ssd-local` | required, `kubernetes.io/hostname` |
| **Proxmox-single**  | 1         | `topology.kubernetes.io/region: proxmox` | `piraeus-ssd-local`                                | n/a                                |
| **Optiplex-single** | 1         | `kubernetes.io/hostname: optiplex`       | `piraeus-ssd-local`                                | n/a                                |

**OVH-HA**: For services co-located with the SeaweedFS cluster on the OVH
nodes. Two instances on separate nodes.

**Placement** (callers choose a `cnpg.ClusterPlacement` preset): pod
anti-affinity is `required` on `kubernetes.io/hostname` for every OVH Cluster,
and the selected `cnpg.ClusterPlacement` profile carries the node pin, storage
class, and control-plane tolerance together. Callers select one of these named
profiles rather than deriving scheduling eligibility from the storage-class
string:

- `cnpg.OVH_PIRAEUS_SSD`: `HIL_OVH`, `piraeus-ssd-local`, with a
  control-plane toleration.

The Piraeus SSD profile allows scheduling onto the current OVH SSD pool nodes,
which are control planes. This is an explicit pairing, not a property inferred
from a class name. **Gotcha:** a local PV pins a running instance to its node;
before changing a profile's tolerance, check where its PV lives and rebuild the
instance on a schedulable node if needed.

**Proxmox-single**: For homelab services. Single instance co-located with the
app on Proxmox. Relies on ZFS for local reliability; off-site backups via
pg_dump CronJobs (see backup strategy in <plan.md>).

**Optiplex-single** (`cnpg.OPTIPLEX_PIRAEUS_SSD`): for a database whose app is
pinned to the OptiPlex at home (Home Assistant's recorder), so writes stay
on-node. Recovery comes from barman-cloud base backups and WAL in SeaweedFS on
OVH, not from a replica.

**Why only these**: CNPG applies affinity uniformly to all instances (primary
and replicas). If a cluster spans regions, a failover can promote the remote
replica, causing all writes to cross the Nebula mesh indefinitely. Pinning
all instances to one site avoids this.

### R3: CNPG storage class follows the pin

OVH-HA uses `piraeus-ssd-local`, or `piraeus-hdd-local` for an HDD-backed DB.
Proxmox-single uses
`piraeus-ssd-local` on wyrm2. OVH-HA gets
replication at the CNPG level (2 instances on separate nodes);
Proxmox-single gets replication at the storage level (ZFS on the Proxmox
host). The selected `ClusterPlacement` keeps the node pin, storage class, and
control-plane tolerance explicit as one choice; changing storage topology
requires reviewing that profile and the bound PVs together.

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

### R6: A cloned cluster still names its application database

A Cluster created by `pg_basebackup` or `recovery` keeps declaring `bootstrap.initdb`
with the cloned `database`, `owner` and credentials `secret` after promotion, as
`cluster/cdk8s/forgejo/db.py` does. Without a `bootstrap`, CNPG defaults to `initdb`
database and owner `app` and keeps using them after creation: the metrics exporter runs
its default queries against that database, and the primary's instance reconcile sets that
owner's password from the secret. When the role does not exist, that reconcile fails and
skips every later step.

## Current Compliance

No roster here — it drifts. The SSOT is the `Cluster` manifests themselves;
sweep them with:

```bash
grep -rl 'kind: Cluster' cluster/k8s --include='postgres-cluster*.yaml'
```

Active Clusters are 2-instance OVH-HA on `piraeus-ssd-local`, except
`attic-db` (`nix-cache/`) and `study-casino-db` (`generated/study-casino/`),
which run 3 instances to tolerate one node loss without quorum or primary
impact.

Parked clusters (retained in Git; R2/R3 bind again on revival):

- `Props` (<../../props/deploy/>): suspended 2026-08-20 for a temporary teardown
  (<decisions.md> § "Parked application manifests").
- Proxmox-single: `firecrawl-db`, `inventree-db`, `tandoor-db` (`x/`). Their
  manifests still name the retired `local-path` StorageClass — re-point to
  `piraeus-ssd-local` when reviving.
- `wayback-archive-db` (<../../loom/wayback/deploy/>): OVH-HA shape,
  retained in the parked Wayback package, outside the active Flux root.

The retired Haku dispatch database source has been removed. Its Flux owner remains
suspended with pruning disabled and `deletionPolicy: Orphan` while any retained CNPG
state receives a separate disposition review; this source cleanup does not delete live
database resources or PVCs.

## TODO

- [ ] Set up off-site backups for Proxmox-single clusters (see "CNPG Backup
      Strategy" in <plan.md>)
- [ ] Machine-check R1: pre-commit or CI check that no `image: postgres:*`
      appears in StatefulSets/Deployments outside of CNPG
- [ ] Machine-check R2: validate that every CNPG Cluster matches an allowed
      profile (instance count, region and storage class)
- [ ] Machine-check R3: validate that every CNPG Cluster uses the storage
      class R3 prescribes for its profile
- [ ] Machine-check R5: validate that app `nodeSelector` region matches its
      CNPG cluster's region
