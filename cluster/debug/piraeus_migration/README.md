# Piraeus local-path migration evidence

The [node-roll runbook](../../docs/runbooks/piraeus_node_roll.md) holds the
operational gates. This index separates the latest reported status from dated
inventories, proofs, and benchmark results. Refresh live state and the runbook
gates before any operation.

## Status snapshot — 2026-10-22

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

The evacuated legacy Seaweed SSD Pod is retired. Every SSD volume retains two
copies on the first two converted nodes; a new 250Gi server on `104963` is proposed
in PR #11624. Master-0 remains healthy on its temporary HDD claim on `102453`.
Attic and Study Casino are restored to three healthy members, with fresh 8Gi and
1Gi Piraeus claims on `104963`, observed WAL replay and all temporary holds removed.
The seven normally two-member PostgreSQL clusters remain healthy on the other
converted nodes. Forgejo Valkey has two 2Gi Piraeus claims; the testing database
was recreated empty on a fresh 5Gi Piraeus claim by agreement (PR #11604).

The HDD copy leg from `103656` to the 4TB `102453` completed 150 native moves.
The remaining 151-copy leg to the smaller `103711`
leg remains held for physical reclamation: deleting S3 objects does not itself
free the volume files. Four initial backup scopes, three additional scopes and
Agentplane staging's approved snapshot were cleared; future backups remain
enabled. Survivor-only compaction targets volumes already on `hdd-0`/`hdd-1`,
which are disjoint from the active `hdd-2` to `hdd-1` copy inputs. Volumes still
on the source in maintenance reject compaction there; do not count their
unreclaimed bytes. Refresh actual disk space, copy/index overhead and growth
before releasing the smaller-destination wave. No HDD reset or ClickHouse
migration has started. The accepted overcommit policy and issue trigger remain
in the [runbook](../../docs/runbooks/piraeus_node_roll.md#accepted-thin-pool-overcommit-policy).

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
