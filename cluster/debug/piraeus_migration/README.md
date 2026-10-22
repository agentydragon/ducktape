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

The last SSD control plane, `ovh-ns104963`, is being evacuated. Its Seaweed SSD
server is online and empty after 35 native moves, with every current SSD
volume retaining two copies on the converted nodes. All nine persistent
PostgreSQL clusters now have two healthy off-node members. Attic and Study
Casino temporarily hold their desired-three templates at two until this node
returns. Forgejo Valkey now has two 2Gi Piraeus claims; the testing database was
recreated with native initdb on a fresh 5Gi Piraeus claim after explicit approval
to discard its data (PR #11604). Master-0, SSD client refresh/retirement, historical
data ownership, and refreshed destructive-window gates remain. No reset of
`104963` has started. See [the drain checkpoint](next_nodes/third_ssd_drain.json).

The HDD copy leg from `103656` to the 4TB `102453` has resumed after a fresh
three-server identity, replication and capacity check. The smaller `103711`
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
