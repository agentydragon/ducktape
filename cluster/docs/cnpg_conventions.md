# CNPG Conventions

Rules for CloudNativePG database clusters in this cluster.

## Rules

### R1: All PostgreSQL must be CNPG

Every PostgreSQL database in the cluster must be a CloudNativePG `Cluster`
resource. No bare StatefulSets, Helm-bundled postgres subcharts, or custom
postgres deployments.

### R2: Allowed configurations

Only these CNPG cluster profiles are permitted:

| Profile                                    | Instances | Pin                                      | Storage                                           | Anti-affinity                      |
| ------------------------------------------ | --------- | ---------------------------------------- | ------------------------------------------------- | ---------------------------------- |
| **OVH-HA**                                 | 2         | `topology.kubernetes.io/zone: hil-ovh`   | `local-path-ovh-hdd` or `-ssd`                    | required, `kubernetes.io/hostname` |
| **OVH-HA with Piraeus pilot replica**      | 3         | `topology.kubernetes.io/zone: hil-ovh`   | Existing claims unchanged; new claim uses Piraeus | required, `kubernetes.io/hostname` |
| **OVH-HA with Piraeus migration template** | 2         | `topology.kubernetes.io/zone: hil-ovh`   | Existing claims unchanged; new claim uses Piraeus | required, `kubernetes.io/hostname` |
| **Proxmox-single**                         | 1         | `topology.kubernetes.io/region: proxmox` | `local-path-proxmox`                              | n/a                                |

**OVH-HA**: For services co-located with the SeaweedFS cluster on the OVH
nodes. Two instances on separate nodes.

**OVH-HA with Piraeus pilot replica**: Only for Attic and Study Casino while
the `piraeus-ssd-local` class has a single local pool on `ovh-ns1001419`. Their
existing two PVCs keep their current StorageClass and size; CNPG applies the
Piraeus class and configured size only to a newly-created claim.

**OVH-HA with Piraeus migration template**: A bounded new-claim profile for
local-storage node evacuations, including the `ovh-ns104952` SSD and
`ovh-ns103656` and `ovh-ns103711` HDD evacuations below. Git keeps the desired count at two and
existing claims unchanged; a new claim uses `piraeus-ssd-local`, sized for
measured PGDATA plus headroom, with `resizeInUseVolumes: false`. The SSD
evacuation uses the temporary third-member procedure in the [Piraeus node roll
runbook](runbooks/piraeus_node_roll.md#postgresql). For the HDD evacuation,
rebuild only the targeted standby while keeping two desired instances; the
primary is briefly the only member. Keep a resource-specific Flux hold until
the template change is merged and reconciled.

**Placement** (callers choose a `cnpg.ClusterPlacement` preset): pod
anti-affinity is `required` on `kubernetes.io/hostname` for every OVH Cluster,
and the selected `cnpg.ClusterPlacement` profile carries the node pin, storage
class, and control-plane tolerance together. Callers select one of these named
profiles rather than deriving scheduling eligibility from the storage-class
string:

- `cnpg.OVH_LOCAL_PATH`: `HIL_OVH`, `local-path-ovh`, no control-plane
  toleration. This preserves the existing behavior for the legacy HDD alias.
- `cnpg.OVH_LOCAL_PATH_SSD`: `HIL_OVH`, `local-path-ovh-ssd`, with a
  control-plane toleration.
- `cnpg.OVH_PIRAEUS_SSD`: `HIL_OVH`, `piraeus-ssd-local`, with a
  control-plane toleration.

The two SSD profiles allow scheduling onto the current OVH SSD pool nodes,
which are control planes. This is an explicit pairing, not a property inferred
from a class name. **Gotcha:** a local PV pins a running instance to its node;
before changing a profile's tolerance, check where its PV lives and rebuild the
instance on a schedulable node if needed.

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
new clusters name the tiered classes explicitly.) The Piraeus pilot profile's
new claim uses `piraeus-ssd-local`; this local, one-node class does not replace
the existing replicas' bound volumes. The bounded Piraeus migration-template
profile uses `piraeus-ssd-local` only for a newly-created claim; existing
replicas stay on their bound volumes. Proxmox-single uses
`local-path-proxmox`. OVH-HA gets
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

Most active Cluster manifests describe 2-instance OVH-HA — most on the
deprecated `local-path-ovh` alias, plus `forgejo-db-ssd` and
`seaweedfs-filer-db-ssd` on the `local-path-ovh-ssd` tier. Current exceptions:

- `attic-db` (`nix-cache/`): 3 instances using the Piraeus pilot profile. Its
  two existing local-path PVCs remain unchanged; the new claim uses
  `piraeus-ssd-local`.
- `study-casino-db` (`generated/study-casino/`): 3 instances using the Piraeus
  pilot profile. Its two existing 1Gi PVCs retain their current classes; the
  new claim uses `piraeus-ssd-local`. This restores its deliberate profile for
  tolerating one-node loss without quorum or primary impact.
- `agentplane-staging/postgres`, `atuin-db`, `authentik-db-ovh`,
  `forgejo-db-ssd`, `langfuse-db`, `seaweedfs-filer-db-ssd`, and
  `tofu-state-db-ovh`: 2 desired instances using the Piraeus migration-template
  profile for the `ovh-ns104952` SSD evacuation. Existing PVCs keep their
  current StorageClass and size; a new claim uses
  `piraeus-ssd-local` sized for measured data and headroom, with
  `resizeInUseVolumes: false`. Create the temporary third member one cluster
  at a time under a resource-specific Flux hold. After switchover, keep the
  Piraeus-seeded member and retire only the exact old member on `ovh-ns104952`;
  restore/verify two desired members before releasing the hold.
- `claude-session-sync-db` (40Gi), `mcp-oauth-state-db` (5Gi), and `ntfy-db`
  (2Gi): 2 desired instances using the same new-claim profile for the
  `ovh-ns103656` HDD evacuation. Rebuild only the targeted standby, briefly
  leaving the primary alone, and verify two healthy members off the target
  before disk wipe. Protect the old PV with reclaim policy `Retain`; remove
  only the old standby PVC and claim metadata needed to force a fresh Piraeus
  claim, leaving the retained PV unused.
  Preserve the primary Pod UID by adding the control-plane toleration in place
  to its Pod and stored `cnpg.io/podSpec` annotation while CNPG is paused.
  Keep a resource-specific Flux hold on each Cluster until this template change
  is merged and reconciled.
- `gatus-db` (2Gi), `grafana-db-ovh` (4Gi), `haku-console-db` (3Gi),
  `haku-mailbox-db` (10Gi), `litellm-db` (5Gi), `matrix-db` (10Gi), and
  `plaid-mcp-db` (5Gi): 2 desired instances using the Piraeus migration
  template for the `ovh-ns103711` HDD evacuation. Existing claims keep their
  current class and size; replacement claims use `piraeus-ssd-local` with
  `resizeInUseVolumes: false`. New sizes are at least 1Gi above measured
  PGDATA including WAL, rounded up to whole GiB; larger existing requests are
  retained. Apply the primary Pod UID preservation rule above before replacing
  each standby.
- `postgres` (`generated/agentplane-testing/`): 1 instance pinned
  `zone: hil-ovh` on `local-path-ovh-ssd`, shared by testing's three disposable
  logical databases. This experimental environment deliberately accepts no
  CNPG-level or storage-level replication; the manifest comment is the record.

Parked clusters (retained in Git; R2/R3 bind again on revival):

- `Props` (<../../props/deploy/>): suspended 2026-08-20 for a temporary teardown
  (<decisions.md> § "Parked application manifests").
- Proxmox-single: `firecrawl-db`, `inventree-db`, `tandoor-db` (`x/`). Their
  manifests still name `local-path`, the chart-default StorageClass retired
  2026-06-03 (`cdk8s/local_path_provisioner.py`) — re-point to
  `local-path-proxmox` when reviving.
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
