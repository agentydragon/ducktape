# HDD worker pair audit for SSD control-plane 104952

Read-only snapshot on 2026-10-22, refreshed through 12:05 UTC. No node rolls,
volume evacuations, repairs, compactions/vacuums, storage changes, or tests were
run in this audit. This note contains selected live fields only; it does not
contain credentials, kubeconfig/Talos config, or raw API exports.

## Recommendation

If choosing one HDD worker for a later pair with `ovh-ns104952`, prefer
`ovh-ns103656`: it is the last Seaweed HDD volume ordinal, has fewer local
stateful consumers, and does not host a ClickHouse data replica. The current
pair is **not roll-ready**. The immediate Seaweed placement gate is unmet:
all 496 HDD volume IDs must have a copy on each of the two surviving HDD
servers for the strict paired-roll plan, while their current configured cap is
400 each. Also complete the app/native-storage migrations and recheck physical
headroom before wiping a data disk.

## Physical disk layouts

Talos `get disks` / `get volumestatus` and `mounts` were queried live. HDD
filesystem figures below are decimal GB and rounded by `talosctl mounts`.

| Node                                    | Current system disk                                                                                                                        | Current data disk                                                                                   | Current data filesystem total / used / free |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- | ------------------------------------------: |
| `ovh-ns1001419` (applied Piraeus pilot) | `/dev/nvme0n1`, WDC CL SN720, 512 GB; Talos owns the system volumes                                                                        | `/dev/nvme1n1`, WDC CL SN720, 512 GB                                                                |                 Piraeus LVM_THIN; see below |
| `ovh-ns103656` (candidate)              | `/dev/sda`, HGST HUS726020AL, 2,000,398,934,016 B; `/dev/sda5` is Talos EPHEMERAL XFS (1,996,095,488,000 B); STATE/META also on `/dev/sda` | `/dev/sdb`, HGST HUS726020AL, 2,000,398,934,016 B; Talos `u-local-path-ovh-hdd` is whole-device XFS |             1,999.42 / 1,162.08 / 837.34 GB |
| `ovh-ns102453` (candidate)              | `/dev/sda`, HGST HUS726T4TAL, 4,000,787,030,016 B; `/dev/sda5` is Talos EPHEMERAL XFS (3,996,482,797,568 B); STATE/META also on `/dev/sda` | `/dev/sdb`, HGST HUS726T4TAL, 4,000,787,030,016 B; Talos `u-local-path-ovh-hdd` is whole-device XFS |           3,998.83 / 1,616.42 / 2,382.42 GB |
| `ovh-ns103711` (remaining HDD server)   | Existing Talos system layout                                                                                                               | `/dev/sdb`, 2 TB HDD, whole-device XFS                                                              |             1,999.42 / 1,142.78 / 856.64 GB |

The applied pilot's data disk is separate from its system disk. Live LVM facts
from the applied pilot: PV/VG 512,107,741,184 B; thin pool 503,517,806,592 B;
4 GiB metadata plus 4 GiB metadata spare; 64 KiB chunks; VG free space 0.
Four live PVCs request 263 GiB total; pool data allocation was about 1.78%.
This pilot is not evidence that the same metadata policy is safe for HDD-sized
pools.

**Future HDD proposal, not deployed or yet validated:** after every local claim
and required Seaweed copy is evacuated, retire Talos's whole-device XFS user
volume on the verified `/dev/sdb`, then provision the whole data disk as a
Piraeus LVM_THIN pool. The current repo config is only for the SSD pilot; no
HDD LVM_THIN mapping/pool exists on either candidate. Re-read device identity
at execution and do not carry an old local-path size onto the new pool.

Applying the pilot's 4 GiB metadata + 4 GiB spare and 4 MiB extent model to the
2 TB `103656` disk yields at most **1,855.015625 GiB** thin-data extents before
other VG overhead. Its current bound local PV requests are 1,890 GiB, including
1,800 GiB Seaweed plus 90 GiB other local claims: **34.984375 GiB above** that
modeled pool. A uniform 1,700 GiB HDD Seaweed PVC plus the existing 90 GiB
other requests would be 1,790 GiB, leaving 65.015625 GiB modeled, before
additional overhead and operational reserve. This is only a capacity model;
Legacy local-path requests were not enforced physical quotas, and 4 GiB metadata sizing is not
validated for these larger pools. The thin-pool maximum, metadata growth,
monitoring, and acceptable overcommit need an explicit policy.

## 103656 current local-path requests and path usage

Live bound node-affined hostPath PVs total **1,890 GiB requested** (10 claims).
Talos `usage -d 2` reported the directory byte totals below. These are
**reported directory bytes**, not verified physical allocated blocks. In
particular the 10 GiB coder workspace path reports 10,737,418,262 B (10 GiB +
22 B); this may be a sparse disk image, so migration must preserve sparse
allocation and ensure the destination can hold the file if it fills.

| Current local claim on 103656                                        |  Request | Reported directory bytes |
| -------------------------------------------------------------------- | -------: | -----------------------: |
| `seaweedfs/mount0-seaweedfs-volume-hdd-2`                            | 1,800 Gi |      1,049,229,881,089 B |
| `claude-session-sync/claude-session-sync-db-2`                       |    40 Gi |         11,449,799,881 B |
| `mcp-oauth-state/mcp-oauth-state-db-1`                               |     5 Gi |            669,782,965 B |
| `agentplane-staging/public-coder-1-workspace`                        |    10 Gi |         10,737,418,262 B |
| `ntfy/ntfy-db-2`                                                     |     2 Gi |            631,020,228 B |
| `clickhouse/keeper-data-chk-clickhouse-keeper-keeper-0-0-0` (Retain) |     2 Gi |             50,853,734 B |
| `agentplane-staging/public-coder-ducktape-1-workspace`               |    10 Gi |            101,194,109 B |
| `agentplane-staging/public-coder-1-state`                            |    10 Gi |             58,687,131 B |
| `agentplane-staging/public-coder-ducktape-1-state`                   |    10 Gi |              6,921,853 B |
| `monitoring/db-alertmanager-monitoring-0`                            |     1 Gi |                 24,444 B |

Other current local claim requests sum to 90 Gi; their reported directory bytes
sum to 23,705,702,607 B. The mount also contains older/unmatched `pvc-*`
directories. Their ownership and retention are unresolved, so the full data
mount cannot be treated as disposable after the current bound claims move.

## SeaweedFS placement and current capacity

Current HDD group: **496 unique volume IDs / 992 placements**, two copies per
ID, no under- or over-replicated IDs in the status snapshot. The master and each
server's registered ID set agree (0 missing): hdd-0 has 345, hdd-1 has 346,
and hdd-2 has 301. The volume servers report `maxVolumeCounts=400`.

For two copies on both surviving HDD servers before formatting the target,
each survivor must hold all 496 IDs, requiring +96 slots per survivor (+192
across the pair). A declarative capacity change to at least 496 (root is
considering 600) must be reviewed and applied before evacuation; do not patch
runtime state. Disk capacity does not remove this slot limit.

Latest live master placement and volume-server `Size` sums give the following
copy plan. Each existing target volume has exactly one current copy outside
the target; the destination shown is the other surviving HDD server.

| Target worker    |        Target IDs / bytes |    New copies to 103711 |    New copies to 102453 |    New copies to 103656 |
| ---------------- | ------------------------: | ----------------------: | ----------------------: | ----------------------: |
| `103656` / hdd-2 | 301 / 1,049,104,087,677 B | 151 / 621,425,547,851 B | 150 / 427,678,539,826 B |                       — |
| `102453` / hdd-1 | 346 / 1,041,455,005,900 B | 151 / 459,006,872,612 B |                       — | 195 / 582,448,133,288 B |

Estimated post-copy filesystem free space, subtracting those bytes from the
latest rounded Talos mount free values:

| Target worker | Remaining HDD server | Approx. free after copy | 10% free threshold | Margin above threshold |
| ------------- | -------------------- | ----------------------: | -----------------: | ---------------------: |
| `103656`      | `103711` (2 TB)      |               235.21 GB |          199.94 GB |               35.27 GB |
| `103656`      | `102453` (4 TB)      |             1,954.74 GB |          399.88 GB |            1,554.86 GB |
| `102453`      | `103711` (2 TB)      |               397.63 GB |          199.94 GB |              197.69 GB |
| `102453`      | `103656` (2 TB)      |               254.89 GB |          199.94 GB |               54.95 GB |

These estimates use reported current volume `Size` bytes and Talos free space,
not a guarantee: workload growth, compaction behavior, local non-Seaweed data,
filesystem overhead, and copy/repair overhead can consume the narrow 35–55 GB
margins. Re-read them after any migration and immediately before drain.

The per-server tombstone `DeletedByteCount` sums are hdd-0 42,359,636,579 B,
hdd-1 92,136,468,637 B, and hdd-2 103,445,247,266 B. These are potential
compaction upper bounds, **not currently free bytes**; no vacuum/compaction was
run or included in the estimates. The current group also depends on healthy
masters/repairs and long-lived FUSE client handoff; recheck at execution.

## Workload and control-plane gates

- `103656` is the last HDD Seaweed ordinal and has 3 CNPG member pods; `102453`
  is the middle HDD ordinal, has 23 local-path claims (2,534 Gi requested), a
  ClickHouse data replica, Keeper voter, and Seaweed filer. Prefer `103656` if
  all other gates are equal.
- Latest CNPG snapshot: pair `{103656,104952}` touches 12 clusters, 10 need an
  extra healthy member off-pair, and 7 primaries need moving off the pair.
  Pair `{102453,104952}` touches 19 clusters, 17 need an extra healthy member,
  and 10 primaries need moving. All queried clusters were healthy at snapshot;
  recheck member/primary status after handoff.
- ClickHouse has two data pods: `chi-clickhouse-default-0-0-0` on `103711` and
  `chi-clickhouse-default-0-1-0` on `102453`, each with a Bound 500 Gi local
  PVC. Native `system.replicas` queries on both pods showed 17 tables with
  `is_readonly=0`, `is_session_expired=0`, queue size 0, active/total replicas
  2/2, and lost parts 0. Rolling `102453` would remove one of the two data
  replicas; migrate/add a healthy second copy outside the outage first, then
  recheck native replication. Keeper voters are on `103656`, `103711`, and
  `102453`, each with a 2 Gi PVC; preserve 2/3 quorum during any one-worker
  roll and recheck after migration.
- The old Seaweed master-2 4 Gi PV is Released/Retain on `102453`; preserve it
  until the moved master is accepted and old data ownership is resolved.
- Piraeus controllers currently run on `103656`; the controller datastore is
  Kubernetes and `/var/lib` is emptyDir. For a `103656` roll, move/reschedule
  controller/CSI/affinity pods to `102453`, then verify live Piraeus API and
  metadata health before drain. Current target-worker pools are FILE_THIN
  canaries; no HDD LVM_THIN resource exists there. FILE_THIN success does not
  prove the proposed HDD LVM_THIN lifecycle, recovery, metadata sizing, or
  capacity behavior.
- Before either roll, move/consistency-check all local-path state (including
  coder workspace sparse-file semantics), ClickHouse if `102453` is selected,
  CNPG replicas/primaries, Keeper quorum, filer/Mimir/Loki and other stateful
  pods; verify recovery copies, app readiness, and that no unreconciled directories
  remain on the target disk.
- Recheck etcd/control-plane health, Seaweed masters and repair jobs, current
  node/pod readiness, disk serial/WWN, PV UID/phase/consumer mapping, and
  post-evacuation free space immediately before any destructive reformat.

## Evidence sources

Read-only live queries: Talos `get disks`, `get volumestatus`, `mounts`, and
`usage`; Kubernetes Nodes/PVs/PVCs/Pods/CNPG status; documented Seaweed master
`/vol/status` and per-volume-server `/status`; ClickHouse `system.replicas`.
Repo guidance read: `cluster/docs/runbooks/piraeus_node_roll.md`,
and the current SSD-pilot Piraeus config. No CI/tests were run; none were
requested for this read-only audit.
