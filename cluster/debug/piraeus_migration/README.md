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
needed. Only the canary moved; bulk rebalance and later HDD-node conversions
have not started.
The accepted overcommit policy and issue trigger remain in the
[runbook](../../docs/runbooks/piraeus_node_roll.md#accepted-thin-pool-overcommit-policy).

ClickHouse Keeper replicas 1 and 2 were cold-copied serially to retained
`piraeus-ssd-local` claims on `ovh-ns1001419` and `ovh-ns104952`. Full data and
metadata matched, Raft identities and DNS stayed unchanged, and the leader
reported two synced followers after each copy. Keeper 0 was the final leader.
Quorum and all 17 ClickHouse tables are healthy, with both replica queues at
zero. Replica 0 remains on `ovh-ns104963`; all three Pod UIDs and restart counts
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
primary Pod and stored `cnpg.io/podSpec`. The old secondary files remain on
`103711`, excluded from archives for eventual wipe, and their old `Retain` PV
metadata remains. This work did not authorize a wipe; all temporary Cluster
holds were removed after acceptance.

`ovh-ns103711` remains cordoned for reimage. The user waived preservation of
SeaweedFS source copies on its old HDD volume after verifying healthy survivors,
and the Anki claim. Grocy has been preserved via VolSync, and ClickHouse may
temporarily run with its healthy off-node data replica. The new Piraeus HDD
selector requires `piraeus-ready=true`; keep that label absent and leave the disk
uninitialized until the wipe and physical layout are accepted.

Grocy SF and Vallejo were cold-copied from local-path claims on `ovh-ns103711`
to retained 1Gi `piraeus-hdd-local` claims on `ovh-ns103656` using Rsync-TLS
while both apps were stopped. Source and destination file hashes, permissions,
ownership, and symlink targets matched, and SQLite integrity checks returned
`ok` before and after startup. The existing SeaweedFS backup sources were
retargeted to the Piraeus claims and completed a cold refresh. Both deployments
are Ready on `103656`; the old source PVCs/PVs remain Bound with `Retain`. Their
Flux Kustomizations remain held until the source change is published and merged.

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
