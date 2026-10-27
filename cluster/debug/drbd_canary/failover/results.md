# Results

## 2026-10-27: diskless primary on worker `ovh-ns103711`

Setup: operator v2.12.0, `ha-controller` v1.3.4 on all 8 satellites. Canary
`pvc-fde52587-4fdf-4c33-a94d-02c609218dec` had UpToDate copies on
`ovh-ns1001419` and `ovh-ns104963`, a diskless tiebreaker on `ovh-ns103656`, and
the writer as diskless Primary on `ovh-ns103711` (workers have no `ssd` pool, so
this run tests Pod and attachment failover, not loss of a data copy).

| Time (UTC) | Event                                                                               |
| ---------- | ----------------------------------------------------------------------------------- |
| 16:34:07   | last line written on `ovh-ns103711` (seq 3848); sysrq reset fires                   |
| 16:34:39   | HA controller evicts the Pod (`VolumeWithoutQuorum`), taints the node `lost-quorum` |
| 16:34:39   | replacement Pod scheduled on `ovh-ns103656`; attach blocked by Multi-Attach         |
| 16:34:58   | node controller marks `ovh-ns103711` NotReady                                       |
| 16:35:27   | attach succeeds on `ovh-ns103656`                                                   |
| 16:35:31   | writer resumes (seq 3849)                                                           |
| 16:36:06   | `ovh-ns103711` Ready again; its stale diskless resource is gone from LINSTOR        |
| 16:38:39   | `ha-controller` on `ovh-ns103711` restarts and removes the `lost-quorum` taint      |

- **Write outage: 84 s** (32 s to eviction, 48 s waiting for the old attachment
  to clear, 4 s Pod start). Without the HA controller the Pod would not be evicted
  before the 300 s `unreachable` toleration, then would hang Terminating.
- **Integrity:** 3,970 lines, contiguous sequence, 0 hash mismatches. The new
  writer continued at the file's last line, so this shows no corruption or
  divergence; it cannot show an acknowledged line from the last second before
  the reset was not lost, because that Pod's stdout died with the node.
- **Placement after:** UpToDate on `ovh-ns1001419`, `ovh-ns104963`; InUse
  diskless on `ovh-ns103656` (the former tiebreaker). No faulty resources.
- Unrelated: two volsync `dst-grocy-config-ovh-backup` mover Pods on the node
  were left `Unknown` after the reboot.

Not covered: loss of a node holding a data copy (all OVH `ssd` pools are on the
control-plane nodes), network partition, and resync of a returning diskful copy.
