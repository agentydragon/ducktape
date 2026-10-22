# Piraeus local-path migration evidence

The [production plan](../../docs/plans/piraeus_production.md) describes the
migration sequence. The [node-roll runbook](../../docs/runbooks/piraeus_node_roll.md)
holds the operational gates. This index separates the latest reported status
from dated inventories, proofs, and benchmark results. Refresh live state and
the runbook gates before any operation.

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

After the 600-slot HDD configuration merged at 12:21:34 UTC, the operator
reported the Seaweed volume-pod rollout in progress: `hdd2` was Ready at 600,
`hdd1` was restarting at 600, and `hdd0` still had the old 400-slot template.
The guarded first batch waits for all three pods to be Ready at 600, then checks
two-copy placement and master quorum before moving about 150 copies (about 428
GB) from `ovh-ns103656` to `ovh-ns102453`. The larger batch of about 151 copies
(about 621 GB) to `ovh-ns103711` remains held pending headroom review. This is a
Seaweed volume-pod rollout and copy gate; no OS node roll, reset, or disk wipe
was underway in that status snapshot. The dated 400-slot worker audit remains
unchanged in `next_nodes/worker_pair_audit.md`; the current slot cap is 600 in
[`cluster/cdk8s/seaweedfs/cluster.py`](../../cdk8s/seaweedfs/cluster.py).

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
