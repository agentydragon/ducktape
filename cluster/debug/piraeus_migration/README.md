# Piraeus local-path migration evidence

The [node-roll runbook](../../docs/runbooks/piraeus_node_roll.md) holds the
operational gates. This index separates the latest reported status from dated
inventories, proofs, and benchmark results. Refresh live state and the runbook
gates before any operation.

## Status snapshot — 2026-10-23

The first SSD node, `ovh-ns1001419`, is converted and accepted; application
restores and the Seaweed master-2 handoff are complete. Its retained evidence is
in [`first_node/`](first_node/README.md).

The second SSD control plane, `ovh-ns104952`, is converted and accepted. The
whole data disk backs `piraeus_ssd/data`; filesystem and block PVC checks
survived a controlled reboot. Attic and Study Casino returned on fresh Piraeus
claims, the new 250Gi Seaweed SSD server passed a native move and owned S3
checks, and master-1 returned from the temporary HDD claim. See
[the restoration proof](next_nodes/second_ssd_restoration.json). The older
[reset checkpoint](next_nodes/second_ssd_roll.json) records the pre-reset
boundary, including the retained archive and explicit restore/comparison waiver.

The last SSD control plane, `ovh-ns104963`, is converted and admitted. Its five
system partitions have the agreed sizes; the whole data disk backs a measured
411.18GiB thin-data pool. Filesystem and block PVC hashes and identities survived
a controlled reboot, all three etcd members are healthy, pool monitoring is
fresh, and disposable canaries were removed before uncordoning. See
[the roll proof](next_nodes/third_ssd_roll.json). Historical local-path files were
archived off-node before the wipe; the operator waived scratch restore and file
comparison. Retained source PV metadata does not restore erased disk contents.

The evacuated legacy Seaweed SSD Pod is retired. PR #11624 deployed the new
250Gi Piraeus server on `104963`; a bounded native move passed, with every SSD
volume retaining two copies and existing server/claim identities unchanged.
Master-0 returned to a fresh 4Gi Piraeus claim on `104963`: cold Raft file hashes
and metadata matched, topology identity was preserved, peers kept their Pod UIDs,
and all controllers resumed. Owned S3 probes passed and were removed.
Attic and Study Casino are restored to three healthy members, with fresh 8Gi and
1Gi Piraeus claims on `104963`, observed WAL replay and all temporary holds removed.
The seven normally two-member PostgreSQL clusters remain healthy on the other
converted nodes. Forgejo Valkey has two 2Gi Piraeus claims; the testing database
was recreated empty on a fresh 5Gi Piraeus claim by agreement (PR #11604).
PR #11626 matched Terraform's readiness source to the accepted live node. Its
node-specific apply completed without reboot; the remaining targeted plan has
zero changes (refresh disabled). See [the final restoration proof](next_nodes/third_ssd_restoration.json).
The four application claims total 263Gi against 411.18Gi of thin-data capacity;
the overcommit issue trigger is not met on this SSD node.

OptiPlex is converted and admitted to `piraeus-ssd-local` on its 112Gi NVMe data
partition, which persisted through a controlled reboot. Home Assistant now runs
from `home-assistant-config-piraeus`, and its post-restore VolSync backup was
verified. The historical 32Gi and 1Gi Piraeus claims remain retained.
After GitOps drops its declaration, remove the retired `home-assistant-config`
PVC and erased Retain PV records. The CPAP VM is restored, but the card SSID was absent, so live
sync remains unverified. See the [OptiPlex provisioning record](../../docs/optiplex_provisioning.md)
for the durable disk layout.

### 103656 acceptance checkpoint — 2026-10-23

This checkpoint records the first HDD admission; its volume counts and
destination capacity are historical snapshots.

The SeaweedFS evacuation from `ovh-ns103656/hdd-2` to `ovh-ns103711` is
complete: the 151-copy wave finished at 10:29:33 UTC (final move 1058), and the
source has zero Seaweed volumes and zero allocated volume data. At 10:48 UTC,
the smaller destination had about 331 GB free (16.58%), above the 15%
free-space floor.
The legacy HDD contents were erased under the explicit waiver; no archive was
made. Keeper-0 and Alertmanager-0 were preserved on SSD. Physical acceptance
passed on the whole HDD: `piraeus_hdd/data` is 1851.02GiB with 4GiB metadata, a
4GiB spare and 64KiB chunks. A 512MiB write/fsync completed at 92.5MiB/s;
discard returned pool allocation to 0.01%. Filesystem/block canary hashes and
claim identities survived a controlled reboot without Pod replacement, all
three etcd services remained healthy, and the worker's 32/512/1024/32GiB system
volumes and both disk identities remained intact. The node was admitted and
uncordoned at 12:56 UTC after shared gates passed, local-path aliases were
confirmed to deny new allocations, metrics were fresh, and canaries were removed.
The 1700Gi `piraeus-hdd-local` Retain strict-local Seaweed volume server is
Ready. One uncapped native canary move and the owned S3 probes passed; both
temporary probe objects were deleted. All 504 HDD logical volumes retain two
copies, and existing Pods and claims are unchanged. Pool metrics are fresh and
the target service is up. The Terraform readiness source is merged and its
target apply completed: desired and live configuration match, the
`refresh=false` targeted residual plan has zero changes, and no reboot was
needed. At this checkpoint only the canary had moved; bulk rebalance and later
HDD-node conversions had not started. The subsequent `103711` rebuild is below.

## 103711 admission and application restoration — 2026-10-24

Evacuation handoffs are accepted. ClickHouse host-0 completed its Piraeus
backfill and normal client routing and reconciliation were restored at
`2026-10-24 06:09 UTC`. After peer checks confirmed all 600 logical HDD volume
IDs remained off-node, the 116
user-waived Seaweed source volume copies
were deleted under lock. The empty legacy source Pod is retired; keep legacy
`hdd` out of the operator topology map while its `102453` peer uses
`ordinals.start=1,replicas=1`.

The worker rejoined on Talos `v1.14.0` with the intended custom schematic and
the same Kubernetes Node UID. The `01:12 UTC` system acceptance verified both
disk identities, exact `32/512/1024/32GiB` `EPHEMERAL/CRI/KUBELET/LOG`
partitions, no legacy user volume, and a partitionless, unowned data disk.
After those checks, `piraeus-ready=true` enabled the hostname-specific HDD
configuration. Native LVM and fresh collector metrics
verified a `1,991,807,860,736`-byte `piraeus_hdd/data` thin-data pool with
`4GiB` metadata, a `4GiB` spare, `64KiB` chunks and runtime
`ExtCmd/WaitTimeout=180000`.

Two owned `2Gi` filesystem/block canaries passed `16MiB` write/read hashes,
filesystem `ENOSPC`, and the raw block end boundary at `01:18 UTC`. At
`01:20:40 UTC`, bounded `512MiB` writes and read-back per mode, filesystem trim
and raw block discard passed: native thin-data allocation moved from `0.01%`
to `0.06%` and back to `0.01%`, with the persistent hashes preserved. The first
discard write OOM-killed the canary container; recovery bounded dirty pages with
`fdatasync` and clean-page eviction every `8MiB`.

Controlled-reboot acceptance completed at `01:27:56 UTC`. The boot ID changed;
the Node, canary Pod, PVC, PV and CSI identities and both payload hashes stayed
unchanged. Both disk identities, all four system partitions, thin-pool geometry,
metadata and spare were preserved, and the replacement collector reported
fresh metrics. Post-reboot shared gates passed at `01:28 UTC`; disposable
Pods, claims, PVs, CSI resources and the qualification namespace were removed
at `01:30 UTC`. The targeted Terraform state handoff passed at `01:49 UTC`:
desired/live configuration agreed, the boot ID stayed unchanged, and the locked
`refresh=false` residual plan had zero resource or output changes. Fresh shared
gates, CSI registration, metrics and local-path allocation denial passed before
the worker was admitted and uncordoned at `01:50 UTC`.

The new `1700Gi`/`2000`-slot Seaweed server became Ready, registered empty and
reported metrics `up=1`; existing survivor Pod UIDs were preserved. The new
server was absent from the initial repair pass's topology snapshot. That pass
was stopped with its partial copy cleaned up and original intact; the replacement
copy-only Job `replication-repair-refresh-103711-20261024` now targets `103711`
and refreshes topology after each volume. At `06:29 UTC`, native server
inventories showed all 600 logical IDs present, 540 with two copies and 60
with one copy (about 442GiB still to copy). The same Job's deadline was extended
from 6 to 12 hours at `06:31 UTC` because the observed rate put completion
beyond the original deadline; its Pod identity was preserved. The `replication-repair` AdminScript
is temporarily suspended with its own Flux reconciliation disabled. Restore
its original unsuspended state and remove that annotation after the replacement
Job terminates and native copy/lock checks pass. The new
500Gi ClickHouse host-0 is Ready with its two-database, 26-object catalog
replayed, including two pre-existing orphan materialized views restored with a
per-command compatibility setting. Catalog UUID/definition and migration-ledger
comparison passed at `02:02 UTC`. Final acceptance at `06:09 UTC` found all
17 replicated tables healthy with two active members, no queued work, no parts
to check and no lost parts. All 26 catalog objects still matched, and both
migration ledgers matched with latest version 53 clean. Normal client routing
includes both Ready members, an authenticated query through the Service passed,
the native CHI is `Completed`, and Flux is current and `Ready` without holds.
The operator normalized host-0's Pod-template name, causing one controlled Pod
replacement on the same PVC/PV; the host-1 Pod UID and restart count stayed
unchanged. All three Keepers remained healthy. AIQuota and Langfuse returned
to normal Flux management with clean startup DDL checks. Anki's Flux
reconciliation observed
`64b7c6ea` and is Ready.

The initial two Bound claims totaled 2200Gi. Adding two 2Gi Valkey replicas
during the `102453` evacuation brought the total to 2204Gi. The two 10Gi
monitoring destinations bring commitments to 2224Gi against 1855.015625GiB of
thin-data capacity.
[#11786](https://github.com/agentydragon/ducktape/issues/11786) tracks this live
overcommit and its placement-based exit; low current allocation does not close it.

The shared gate now expects 19 CNPG clusters after merged `2ef4072ceded`
removed `agentplane-testing`; the namespace deletion was observed at
`01:21:16 UTC`. All 19 are Ready at this checkpoint. Seaweed gates require
every prior 600 HDD ID off-target, two copies for every ID outside the approved
116-copy waiver, and any remaining single-copy IDs to be a subset of that
approved set. Healing an approved ID to two copies does not expand the waiver.

Private acceptance artifacts are under
`/tmp/piraeus-hdd-roll-20261023/next-hdd-103711/`:
`system-rejoin-accepted.json`, `pool-geometry-initial-accepted.json`,
`pvc-canaries-before-reboot.json`, `discard-accepted.json`,
`pool-and-reboot-accepted.json`, `system-after-qualification-reboot.json`, and
`pool-geometry-after-reboot.json`, `post-reboot-boundary-gates.json`,
`canary-cleanup-complete.json`, `activation-tf-handoff/apply-accepted.json`, and
`worker-admission-accepted.json`. Refresh live gates before further maintenance.

Operational gotchas from this roll:

- Direct worker `talosctl v1.14.2 upgrade --drain=true` installed the image,
  then failed before reboot when its drain requested the worker's unsupported
  Talos kubeconfig API. A separately gated `reboot --drain=false` completed
  activation on the already-drained worker; the installer was not repeated.
  Verify the booted server version and schematic before selective reset.
- Seaweed S3 gateways retained cached source routing during retirement. Both
  `seaweedfs-s3` and both `public-s3` Pods were refreshed serially, preserving
  a Ready peer. Include gateway refresh alongside FUSE refresh and owned S3
  hash checks before relying on a quiet retired source.

During retirement, `volumeServer.leave` made `/healthz` return `503`; six
liveness failures at a `90s` period restarted the container at
`2026-10-24 00:28:11 UTC` and remounted 116 preserved volume copies. The locked
native `volume.delete` removed only those explicitly waived copies; heartbeat
and health stayed green with maintenance enabled. Avoid `leave` while copies
must remain preserved.
The accepted overcommit policy and issue trigger remain in the
[runbook](../../docs/runbooks/piraeus_node_roll.md#accepted-thin-pool-overcommit-policy).

ClickHouse Keeper replicas 1 and 2 were cold-copied serially to retained
`piraeus-ssd-local` claims on `ovh-ns1001419` and `ovh-ns104952`. Full data and
metadata matched, Raft identities and DNS stayed unchanged, and the leader
reported two synced followers after each copy. Keeper 0 was the final leader.
That handoff finished with healthy quorum and all 17 data tables caught up;
the later data host-0 rebuild above completed catch-up at `06:09 UTC`.
Keeper replica 0
remains on `ovh-ns104963`; all three Keeper Pod UIDs and restart counts
stayed unchanged through Flux reconciliation. The old HDD claims remain bound
with `Retain`, and temporary helpers are removed. Flux is resumed and `Ready`;
the live Keeper spec matches the merged generated manifest, and no holds remain.

Seven CNPG clusters were restored to `2/2` with native, caught-up Piraeus SSD
secondaries off `ovh-ns103711`; all primary PVCs are unchanged, and six primary
Pod UIDs were preserved. Each secondary streamed and replayed the observed
primary LSN with zero current lag. Gatus's primary auto-rolled once
(14:21:44–14:24:58) after the merged control-plane toleration triggered an
unsupervised CNPG primary rollout. Before replacing the remaining six standby
PVCs, each Cluster was briefly held while the toleration was added to its live
primary Pod and stored `cnpg.io/podSpec`. At handoff, the old secondary files
were left on `103711`, excluded from archives; their old `Retain` PV metadata
remains. The subsequent physical rebuild erased the old source disk contents.
All temporary Cluster holds were removed after handoff acceptance.

The `103711` preservation boundary included the user's waiver for
SeaweedFS source copies on its old HDD volume after verifying healthy survivors,
and the Anki claim. Grocy was preserved via VolSync, and ClickHouse may
temporarily run with its healthy off-node data replica. Retained source PV
records do not restore erased files; recovery uses the accepted off-node
handoffs and retained preservation artifacts.

Grocy SF and Vallejo were cold-copied from local-path claims on `ovh-ns103711`
to retained 1Gi `piraeus-hdd-local` claims on `ovh-ns103656` using Rsync-TLS
while both apps were stopped. Source and destination file hashes, permissions,
ownership, and symlink targets matched, and SQLite integrity checks returned
`ok` before and after startup. The existing SeaweedFS backup sources were
retargeted to the Piraeus claims and completed a cold refresh. Both deployments
are Ready on `103656`; the old source PVCs/PVs remain Bound with `Retain`. Their
Flux Kustomizations have resumed and observed the merged configuration; both
remain Ready on their Piraeus claims.

## 102453 evacuation — 2026-10-24

Mimir ingester-0 and Loki writer-0 were cold-copied serially to retained 10Gi
`piraeus-hdd-local` claims on `103711`; handoff finished at `06:53 UTC`. Both
services are `2/2` with their peer on `103656`, whose Pod UID and restart count
stayed unchanged. Full contents and metadata matched before restart and again
through the rebound claim; only host/mount `security.selinux` labels were
excluded. Original source PVs are Released with `Retain`, and private cold
archives remain off-node. Both Flux/HelmRelease owners are current and Ready
without maintenance holds.

Mimir replay/corruption checks passed and buffered writers caught up with no
new failed samples. Recent/historical and outage-window aggregate queries
passed for both services. Alloy failure/drop deltas stayed zero. Promtail
dropped 6,609 entries (111,487,442 bytes) during the Loki move; the user
accepted that migration-specific log gap. The earlier 971-entry count omitted
two temporarily unready senders; all eleven retained their UIDs and returned
Ready. Native Loki WAL replay completed with no corruption or write failures.
The immutable local-path claim templates/defaults remain tracked in #4717.

Fresh source inventory at `06:52 UTC` has four active local-path claims:
Seaweed HDD-1, ClickHouse host-1 and active Coder-1 workspace/state. Two
historical migration archives and parked Coder/recording claims still need
preservation; the old Keeper claim needs replacement-backed metadata cleanup.
All 20 CNPG clusters are Ready, including the new Home Assistant database
created by separate work at `06:30 UTC`; the earlier 19-cluster checkpoints
remain historical. Refresh the expected set before the next physical gate.
Private proofs: `last-hdd-102453/monitoring-evacuation/`,
`last-hdd-102453/root-monitoring-final-accepted.json` and
`last-hdd-102453/remaining-source-live-inventory.json` under
`/tmp/piraeus-hdd-roll-20261023/`.

All ten primaries were switched serially to their caught-up Piraeus SSD peers
before cordoning `ovh-ns102453` at `02:57 UTC`. CNPG can automatically switch
primaries away from an unschedulable node, so cordoning first would have started
several switchovers together. Each old source instance and PVC was then retired
serially; CNPG rebuilt its replacement from the healthy primary using the merged
`piraeus-ssd-local` template. Independent acceptance at `03:22 UTC` verified all
ten at `2/2`, native observed WAL replay and streaming, correct read-write
endpoints and all twenty member claims on strict-local retained Piraeus SSD
volumes. No CNPG Pods remain on the source, no per-cluster reconciliation holds
remain, and all 19 cluster-wide CNPG clusters are Ready. Source PV metadata and
files remain retained.

The node stays cordoned. ClickHouse host-0 catch-up is accepted. Seaweed copy
repair, ClickHouse host-1 and authored/historical data still need acceptance
before a physical reset. Private per-cluster proofs are in
`/tmp/piraeus-hdd-roll-20261023/last-hdd-102453/`, including
`root-final-independent-acceptance.json`.

### Seaweed HDD-1 repair and evacuation

Flux owns all three AdminScripts: this PR makes `repair-hdd-102453` source-active,
and leaves `evacuate-hdd-102453` plus the regular hourly `replication-repair`
source-suspended. Both maintenance scripts lock and set the source to
`maintenanceOn`, which prevents new allocations there while allowing reads and
outgoing copies. Verify the master's source state and the owned S3
write/read/delete probe before copies begin; if that probe fails, restore
`maintenanceOff` only after checking the source state. A successful repair Job
is insufficient because `volume.fix.replication` can return without progress.
Before a follow-up PR enables evacuation, take a fresh full census of every
volume still held by the source. Verify no original volume ID is missing, every
source-held volume has at least two distinct healthy locations under normal
replication policy `001`, and none is erasure-coded. Do not use only the
historical 29-singleton list as this gate. That PR must suspend the repair script
and enable only `evacuate-hdd-102453`. Its CronJob retries failed runs with Job
backoff and its 10-minute schedule; do not add an internal shell retry, `-target`,
or `-skipNonMoveable`. SeaweedFS verifies each new target before deleting that
volume's source copy, so the source Pod and PVC remain until the off-source
replicas and client-refresh gates pass. Keep `maintenanceOn` through retirement;
refresh FUSE clients while the source server is still alive. A later cleanup PR
removes both maintenance AdminScripts and resumes hourly copy-only repair after
placement is stable. Leave the legacy `hdd` group unchanged. Monitor thin pools
throughout: the accepted projection is 475 GiB free (25.6%) on `103656` and
248 GiB free (13.4%, below the 15% floor) on `103711`.

### ClickHouse replica 1 on 103656 — 2026-10-24 (in progress)

Replica 1 started on a fresh 500Gi `piraeus-hdd-local` claim on
`ovh-ns103656`; its target storage proof passed. Its two-database, 26-object
catalog was restored, and the accepted application window verified a clean
106-row migration ledger (latest version 53), plus 17 writable tables with two
active replicas and no lost parts. After a controlled stop and restart, native
copy resumed with the same PVC, PV and CSI identity. At the 08:26 UTC acceptance,
it had 640 queued tasks and 16 active fetches, so catch-up is still incomplete.
Source replica 0 remains Ready with its original Pod UID on
`ovh-ns103711`; client routing remains pinned there. The ClickHouse Flux
Kustomization and native CHI are the remaining holds until full catch-up and
final application gates pass. The former replica-1 Retain claim, PV and files
are preserved.

AIQuota and Langfuse are Ready under normal Flux management. Langfuse web is
Ready on SSD control-plane `ovh-ns1001419` using its existing placement policy.
Both `103656` and `103711` are uncordoned; `102453` remains cordoned while
Seaweed repair continues. Private proofs are under
`/tmp/piraeus-hdd-roll-20261023/last-hdd-102453/clickhouse-replica1/`, including
`root-start-handoff-accepted.json` and `app-window-complete.json`.
Refresh application gates before restoring normal ClickHouse routing or
reconciliation.

OCI-cache and Langfuse Valkey were then evacuated serially: coordinated native
failover to each existing Piraeus member on `103656`, followed by rebuilding
ordinal 1 on a retained 2Gi `piraeus-hdd-local` claim on `103711`. Independent
acceptance at `04:03`/`04:06 UTC` verified `2/2`, replay through an observed master
offset, matching replication IDs, healthy AOF, correct role-specific Service
endpoints and normal Flux/operator ownership with no holds or temporary selector.
Both original ordinal-0 Pod UIDs survived; old source PVs/files remain retained.
No Valkey Pod remains on `102453`. The shared immutable claim templates still
select local-path; [#4717](https://github.com/agentydragon/ducktape/issues/4717)
tracks their default/ownership handoff before retirement.
OCI's initial native promotion preceded its Service-label correction by about
three minutes. Langfuse's promotion and routing changes were batched together;
future switches must use that sequence. Private proofs are under
`valkey-evacuation/root-{oci,langfuse}-final-accepted.json`.

The user-approved inactive Sandbox `agentplane-staging/public-coder-ducktape-2`
was removed through its normal teardown. Both owned workspace/state PVCs and PVs
are gone; a direct Talos filesystem listing confirmed both backing directories
absent at `03:45 UTC`. Active `public-coder-ducktape-1` kept its Pod UID and
remained Ready. Other parked Coder state, recordings and historical archives
remain outside this deletion scope. Private proof:
`inactive-coder-delete-accepted.json` under the artifact root above.

After [the Piraeus controller placement change (#11811)](https://github.com/agentydragon/ducktape/pull/11811)
reconciled, acceptance at `07:20:42 UTC` found all four controller Pods Ready
with zero restarts: affinity on `ovh-ns1001419`, LINSTOR on `ovh-ns104952`, CSI
on `ovh-ns104963`, and the operator on `ovh-ns103711`. All seven leader leases
transferred; seven native LINSTOR nodes stayed `Online`, all 73 native resources
were `UpToDate`, and all 73 Piraeus PV UIDs and CSI-spec hashes were unchanged.
`LinstorCluster` generation 3, both HelmReleases, and both Flux Kustomizations
were current and Ready. `ovh-ns102453` remains cordoned. Private proof:
`last-hdd-102453/controller-relocation/all-controllers-relocated-accepted.json`
under `/tmp/piraeus-hdd-roll-20261023/`.

## Evidence map

| Folder                                    | Contents                                                                                 |
| ----------------------------------------- | ---------------------------------------------------------------------------------------- |
| [`inventory/`](inventory/README.md)       | 2026-10-19 cluster, node, PV-consumer, and storage inventory.                            |
| [`first_node/`](first_node/README.md)     | First-node Talos/Piraeus acceptance, application restores, and SeaweedFS handoff proofs. |
| [`next_nodes/`](next_nodes/README.md)     | 2026-10-22 SSD evacuation proof and dated HDD worker comparison.                         |
| [`ovh_ns104952/`](ovh_ns104952/README.md) | Target-node local-PV consumers, disk identities, and inert configuration proposal.       |
| [`benchmarks/`](benchmarks/README.md)     | Dated FILE_THIN, local-path, and SeaweedFS experiments with scripts and raw results.     |

Child reports retain their capture dates and historical observations. Benchmark
results are supporting evidence, not a substitute for the live capacity and
replica checks in the runbook.
