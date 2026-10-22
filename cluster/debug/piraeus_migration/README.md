# Piraeus local-path migration evidence

The [node-roll runbook](../../docs/runbooks/piraeus_node_roll.md) holds the
operational gates. This index separates the latest reported status from dated
inventories, proofs, and benchmark results. Refresh live state and the runbook
gates before any operation.

## Status snapshot — 2026-10-22

The first SSD node, `ovh-ns1001419`, is converted and accepted; application
restores and the Seaweed master-2 handoff are complete. Its retained evidence is
in [`first_node/`](first_node/README.md).

The early SSD evacuation from `ovh-ns104952` is complete: 33 native moves left
36 current logical SSD volume IDs with two copies on `ovh-ns104963` and
`ovh-ns1001419`. The four affected SSD FUSE clients were refreshed and checked,
then the empty old volume-server Pod was retired through PR #11586. Master-1
moved to `102453`, and Forgejo Valkey replica-1 now uses a 2Gi Piraeus claim on
`1001419`. PostgreSQL handoffs are proceeding serially; Attic and Study already
promoted their existing Piraeus members. The current service proofs, remaining
handoffs and pre-wipe data-preservation findings are in
[`next_nodes/`](next_nodes/README.md).

The inert `ovh-ns104952` storage configuration and its dated local-PV/disk
inventory remain in [`ovh_ns104952/`](ovh_ns104952/README.md). No subsequent node
reset, data-disk wipe or reformat has started. The audit found seven unmatched
old claim directories; preserve them off-node before destruction.

The HDD slot cap is now 600 and the first copy batch from `103656` to the 4TB
node `102453` is running with per-move copy, quorum, identity and 15% free-space
gates. It has resumed only after fresh checks following two read/transport
failures. The approximately 621GB wave to the smaller `103711` remains held:
projected free space is 11.7%, below the 15% reserve. The separate proposal to
move one native ClickHouse data replica to SSD is recorded in `next_nodes/`;
no ClickHouse migration has started. The accepted temporary-overcommit policy
and issue trigger remain in the
[node-roll runbook](../../docs/runbooks/piraeus_node_roll.md#accepted-thin-pool-overcommit-policy).

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
