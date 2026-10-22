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
`ovh-ns1001419`. No OS node roll or data-disk conversion is part of that
evacuation. The dated proof and worker comparison are in
[`next_nodes/`](next_nodes/README.md).

The inert `ovh-ns104952` storage configuration and its 11:46 UTC local-PV and
disk inventory are documented in [`ovh_ns104952/`](ovh_ns104952/README.md).
Its readiness gates remain separate from the completed SSD evacuation.

After the 600-slot HDD configuration merged at 12:21:34 UTC, all three HDD
volume-server pods reached Ready at 600 with stable Pod UIDs. The first batch
from `ovh-ns103656` to `ovh-ns102453` stopped safely after nine of about 150
copies (about 428 GB) when a read-only master-0 quorum check timed out after
180 seconds, before copy 10 started. A fresh preflight at 12:35 UTC then
verified the original pod UIDs, two-copy placement for all current logical
volume IDs, agreement across all three masters, the nine completed placements,
and destination free space above the 15% reserve. Move 10 (volume 934) started
after those checks, with 141 copies remaining in this batch. The earlier timeout
was not treated as quorum evidence. The larger batch of about 151 copies (about
621 GB) to `ovh-ns103711` remains held pending headroom review. No OS node roll,
reset, or disk wipe was underway. The dated 400-slot worker audit remains
unchanged in `next_nodes/worker_pair_audit.md`; the current slot cap is 600 in
[`cluster/cdk8s/seaweedfs/cluster.py`](../../cdk8s/seaweedfs/cluster.py). The
accepted thin-pool overcommit policy and sizing proposal are in the
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
