# Storage Strategy

Part of <plan.md>.

## Why Region-Explicit Storage

No distributed storage system can provide fast local writes AND
cross-site durability simultaneously. This is a consequence of the
CAP theorem: if two sites are separated by latency, you must choose:

- **Wait for remote ack** (consistency) → every write pays the
  cross-site round-trip (~20ms VPS↔Proxmox over Nebula)
- **Don't wait** (availability) → risk losing data written since last
  successful replication if the writing site dies

This applies equally to Longhorn, Ceph, DRBD, or any synchronous
replication system. Async-first systems (JuiceFS+MinIO) avoid the
write penalty but accept a durability risk window.

Therefore: **every PVC must explicitly declare its region.** No
"magic" storage class that works everywhere without tradeoffs.

## Storage Policy

### Storage Classes

Remove the generic `local-path` default. Replace with region-explicit
classes. No default StorageClass — PVCs without an explicit class fail
at creation time rather than silently landing on a random node.

| StorageClass         | Provisioner               | Region  | Use Case                       |
| -------------------- | ------------------------- | ------- | ------------------------------ |
| `local-path-hetzner` | rancher.io/local-path     | Hetzner | CNPG VPS-HA, ephemeral VPS     |
| `local-path-proxmox` | rancher.io/local-path     | Proxmox | CNPG Proxmox, ephemeral PVE    |
| `proxmox-csi-retain` | csi.proxmox.sinextra.dev  | Proxmox | Durable Proxmox data           |
| `hcloud-volumes`     | csi.hcloud.cloud          | Hetzner | Durable VPS data               |
| (future) distributed | ceph-rbd / juicefs / etc. | TBD     | If validated, site-local pools |

### Rules

1. **Every PVC must specify `storageClassName` explicitly.** No default
   StorageClass. Enforced by removing the `is-default-class` annotation.
2. **Apps must be pinned to the same region as their storage.** Extends
   CNPG R4 to all storage, not just databases.
3. **No cross-site synchronous replication.** Storage classes replicate
   within a site only. Cross-site protection is async backup/mirror.
4. **CNPG stays on `local-path-{region}`.** App-level replication
   handles durability. See `cnpg-conventions.md`.
5. **Ephemeral data is explicitly marked.** Prometheus, Grafana, Tempo
   on `local-path-{region}` — rebuildable, not precious.

## PVC Backup / Async Cross-Site Replication

Per-workload backup strategy (no one-size-fits-all):

| Data Type                       | Backup Method                                          | Cross-Site? |
| ------------------------------- | ------------------------------------------------------ | ----------- |
| CNPG databases                  | pg_dump CronJob to remote site PVC (existing pattern)  | Yes         |
| CNPG databases                  | (future) CNPG `ScheduledBackup` + Barman to S3/MinIO   | Yes         |
| CNPG databases                  | (future) CNPG read-only replica cluster at remote site | Yes         |
| Loki/Tempo (if on MinIO)        | MinIO site replication (async)                         | Yes         |
| Harbor registry                 | MinIO site replication (if migrated) or rsync CronJob  | Possible    |
| Ephemeral (Prometheus, Grafana) | No backup needed — rebuildable                         | No          |
| etcd                            | talos-backup CronJob to S3 with age encryption         | Yes         |

### CNPG Cross-Site Read Replica

CNPG convention R2 says "all instances in one region" because CNPG
applies affinity uniformly and a cross-region failover would move the
primary to the wrong site. But CNPG supports **replica clusters** — a
separate `Cluster` CRD with `replica.source` pointing to the primary
cluster via streaming replication.

A replica cluster is a **separate CNPG Cluster** that:

- Streams WAL from the primary cluster continuously
- Is always read-only (cannot be promoted automatically)
- Can have its own `nodeSelector` (different region)
- Can be manually promoted to primary in a disaster (explicit action)

This gives us async cross-site backup for databases without violating
R2. The replica cluster on Proxmox has near-real-time data but never
auto-promotes. If VPS dies, you can manually promote it.

TBD: validate this works with CNPG `replica` mode and whether the
streaming replication across Nebula (20ms) causes any issues.

## Current Longhorn Usage

All Longhorn PVCs (checked 2026-04-01):

| Namespace  | PVC                             | Size | Notes                      |
| ---------- | ------------------------------- | ---- | -------------------------- |
| gitea      | data-gitea-postgresql-0         | 10G  | Legacy, suspended          |
| harbor     | data-harbor-redis-0             | 1G   |                            |
| harbor     | data-harbor-trivy-0             | 5G   |                            |
| harbor     | database-data-harbor-database-0 | 1G   | Legacy (migrated to CNPG?) |
| harbor     | harbor-jobservice               | 1G   |                            |
| harbor     | harbor-registry                 | 30G  |                            |
| loki       | storage-loki-stack-0            | 20G  |                            |
| monitoring | db-alertmanager-monitoring-0    | 1G   |                            |
| monitoring | db-prometheus-monitoring-0      | 20G  |                            |
| monitoring | grafana                         | 2G   |                            |
| monitoring | storage-tempo-0                 | 10G  |                            |
| vault      | vault-raft-instance-{0,1,2}     | 2G   | Goes away if Vault drops   |

None of these benefit from Longhorn's cross-node replication:

- Harbor, Loki: Proxmox-pinned (proxmox-csi would work)
- Vault: Has its own Raft replication. Goes away if dropped.
- Monitoring (Prometheus, Grafana, Tempo, AlertManager): Ephemeral /
  rebuildable. local-path is fine.
- Gitea: Suspended.

## Distributed Storage Options Considered

Goal: replicated storage so workloads aren't pinned to specific nodes,
POSIX multi-writer (RWX) mounting, optionally async replication across
sites for DR.

| Solution           | RAM/node    | RWX / POSIX      | Async WAN  | Fit                                              |
| ------------------ | ----------- | ---------------- | ---------- | ------------------------------------------------ |
| Longhorn (current) | 500-700 MB  | NFS (fragile)    | S3 backup  | Bad: memory hog, issues on wyrm2                 |
| Rook/Ceph          | 1.5-3 GB    | CephFS (native)  | rbd-mirror | Heavy but standard; needs validation             |
| Piraeus/LINSTOR    | 150-300 MB  | RWO only         | DRBD-A     | OK but no RWX; needs DRBD kernel module on Talos |
| OpenEBS Mayastor   | 500 MB-1 GB | RWO only         | No         | Bad: hugepages, no async                         |
| Kadalu (GlusterFS) | 300-500 MB  | Native POSIX     | Geo-rep    | Bad: GlusterFS abandoned by Red Hat              |
| JuiceFS            | 200-300 MB  | Full POSIX RWX   | Yes        | Best async fit — investigate                     |
| SeaweedFS          | 300-500 MB  | FUSE (POSIX-ish) | Yes        | More object store than FS                        |
| MinIO              | 300-500 MB  | S3 only          | Yes        | Not a filesystem                                 |

## Option: JuiceFS + MinIO Site Replication

Full architecture for site-independent, unpinned RWX storage with
async cross-site replication.

**Components:**

- JuiceFS CSI driver (Helm chart: controller StatefulSet + DaemonSet)
- JuiceFS metadata: dedicated database in CNPG (VPS-HA, 2 instances)
- MinIO at Proxmox: data backend, storage on proxmox-csi-retain
- MinIO at VPS: data backend, storage on hcloud volume
- MinIO site replication: active-active async between both instances
- HAProxy DaemonSet: each node prefers local MinIO, falls back to remote

**MinIO site replication details:**

- Two separate MinIO instances (NOT one cluster across sites)
- Truly active-active: both sites accept writes simultaneously
- Async by default. Writes complete locally, replicate in background.
- Conflict resolution: last-write-wins (fine for JuiceFS — objects are
  content-addressed chunks)
- Recovery after outage: background scanner re-queues missing objects.
  For extended outages run `mc admin replicate resync`.
- MinIO docs say site replication requires "distributed deployments"
  (single-node-multi-drive minimum). Each site needs multiple
  drives/partitions for erasure coding.

**JuiceFS failover:** JuiceFS connects to a single S3 endpoint. HAProxy
DaemonSet on each node prefers local MinIO, falls back to remote.
JuiceFS, Loki, Tempo all connect to `localhost:9000`.

**Data flow:**

```text
Pod on VPS → JuiceFS FUSE → metadata: CNPG (VPS-HA)
                           → data: HAProxy → MinIO-VPS (local)
                                    ↕ async site replication ↕
Pod on Proxmox → JuiceFS FUSE → metadata: CNPG (over Nebula)
                               → data: HAProxy → MinIO-Proxmox (local)
```

**Cost:** MinIO ~100 MB RAM per instance. VPS hcloud volumes EUR
0.044/GB/mo. For ~100 GB = ~EUR 4.40/mo.

**JuiceFS project health:** 13.4k GitHub stars, Apache 2.0, actively
maintained. CSI driver at 289 stars, frequent releases. Community
edition is fully sufficient.

**Good workloads:** Harbor registry (30G), Loki logs (20G), Tempo
traces (10G), shared data across pods, anything moving VPS↔Proxmox.

**Bad workloads (keep on local-path):** CNPG databases, latency-sensitive
transactional workloads.

## Option: Rook/Ceph

Standard answer for "nodes should be cattle, not pets."

**What Ceph provides:**

- PVCs that follow pods to any node, no babysitting
- CephFS for full POSIX RWX (native, not FUSE)
- RBD for block storage with sync replication within a site
- rbd-mirror for async cross-site DR
- Battle-tested (CNCF graduated, 20+ years, massive community)
- One system for everything (block, filesystem, object via RGW)

**Cross-site write latency problem (same as Longhorn):**

One of the main reasons for switching off Longhorn is its cross-site
write latency. Ceph with cross-site replicas has the **same
fundamental problem.**

With a pool spanning VPS + Proxmox (size=3: 2 VPS + 1 Proxmox,
min_size=2): every write must wait for at least 2 replica acks.
Regardless of which site the pod is on, at least one ack crosses
the 20ms Nebula link.

Setting `min_size: 1` removes the cross-site penalty but weakens
durability.

A single "works everywhere" pool with cross-site replicas does NOT
solve the Longhorn latency problem. To get fast writes, you still
need site-local pools, which reintroduces per-site StorageClass
management. Only async-first architectures (JuiceFS+MinIO) avoid
this tradeoff.

**Resource concern:** Ceph MON+OSD+MGR+MDS needs 1.5-3 GB/node.
On 8 GB CCX13 that's 20-40% of RAM. Dropping Longhorn + Vault +
Authentik→Authelia reclaims ~2 GB, possibly enough.

**Needs validation** — see Ceph Validation Plan below.

### Ceph Validation Plan

Test in a disposable cluster before adopting in production.

Phase 0 — Disposable test cluster:

1. Provision 3-4 small Talos VMs (cheap/temporary)
2. Install Rook operator + Ceph cluster with minimal config
3. Measure actual resource usage (MON, OSD, MGR, MDS) on small nodes

Phase 1 — Basic block storage (RBD):

4. Create CephBlockPool with `size: 2`, provision PVCs, run test pods
5. Kill a node, verify pod reschedules and PVC reattaches
6. Measure write latency and IOPS vs local-path

Phase 2 — CephFS (POSIX RWX):

7. Create CephFilesystem, deploy MDS
8. Mount from multiple pods simultaneously, verify RWX

Phase 3 — Cross-site topology:

9. Add nodes from a second site
10. Configure CRUSH rules to keep replicas within a site
11. Set up rbd-mirror for async cross-site replication
12. Test site failover

Phase 4 — Observability stack on Ceph:

13. Deploy Loki + Tempo with Ceph RGW (S3-compatible) as backend
14. Verify monitoring stack survives node replacement

Phase 5 — Production sizing:

15. Determine if CCX13 (8 GB) is sufficient or need CCX23 (16 GB)
16. Plan Longhorn → Ceph PVC migration

## Proposed Storage Architecture

**Provisional decision**: Evaluate Rook/Ceph first (validation plan
above). Fall back to JuiceFS + MinIO if Ceph resource overhead is too
high for 8 GB nodes.

| Use Case                    | Storage            | Notes                                       |
| --------------------------- | ------------------ | ------------------------------------------- |
| Databases (CNPG)            | local-path         | App-level replication, per CNPG conventions |
| Metrics (VictoriaMetrics)   | local-path         | Built-in replication (factor=2)             |
| Logs (Loki Simple Scalable) | MinIO (direct)     | Native S3 backend, replication via MinIO    |
| Traces (Tempo)              | MinIO (direct)     | Native S3 backend, replication via MinIO    |
| Replicated / multi-writer   | JuiceFS (on MinIO) | POSIX RWX, metadata in CNPG                 |
| Bulk data (Harbor registry) | JuiceFS (on MinIO) | Unpinned, durable                           |
| Ephemeral (Grafana)         | local-path         | Rebuildable, not precious                   |
| Vault                       | Goes away          | Dropping Vault                              |

Loki and Tempo support S3/MinIO natively — they talk to MinIO
directly via HAProxy, no JuiceFS layer needed.

### MinIO + JuiceFS Validation Plan

Phase 1 — MinIO site replication:

1. Deploy MinIO on Proxmox + VPS
2. Configure site replication
3. Write objects from both sites, verify replication

Phase 2 — HAProxy failover:

4. Deploy HAProxy DaemonSet with local-preference
5. Test failover: kill each MinIO, verify fallback + resync

Phase 3 — Loki + Tempo on MinIO:

6. Reconfigure Loki + Tempo to use MinIO backend
7. Verify data survives a MinIO-site failover

Phase 4 — JuiceFS on MinIO:

8. Deploy JuiceFS CSI, create CNPG metadata database
9. Verify Talos FUSE support (known issue #1083)
10. Test RWX, failover, performance

Phase 5 — Migrate workloads:

11. Migrate Harbor, remaining Longhorn PVCs
12. Decommission Longhorn

**Open items:**

- JuiceFS metadata RO routing to CNPG read replicas
- MinIO multi-drive requirement (hcloud volumes for VPS side)
- JuiceFS mount pod resource tuning (default 512 Mi may be excessive)
