# Piraeus migration evidence

The OVH conversion finished on 2026-10-24: every OVH Talos node and the OptiPlex
run whole-data-disk Piraeus pools. Its lessons are in
[the migration lessons](../../docs/lessons_learned/2026_10_24_piraeus_ovh_migration.md);
the deployed design is [Piraeus block storage](../../docs/piraeus.md) and the
remaining work is the [production plan](../../docs/plans/piraeus_production.md).
Per-node acceptance proofs are in git history.

What remains here supports the open phases:

| Path                                   | Contents                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------- |
| [`inventory/`](inventory/README.md)    | 2026-10-19 cluster, node, PV-consumer and storage inventory, including Wyrm2 disks. |
| [`benchmarks/`](benchmarks/README.md)  | Dated FILE_THIN, local-path and SeaweedFS experiments with scripts and raw results. |
| [`observability.md`](observability.md) | Piraeus disk-accounting contract and its pending Grafana verification.              |

These are dated captures; refresh live state before acting on them.
