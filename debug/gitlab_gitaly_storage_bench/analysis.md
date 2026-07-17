# GitLab + Gitaly Cluster storage bench — results (2026-07-17)

Continuation of the git-storage-latency program: <../forgejo*git_write_latency.md>
(SeaweedFS RWX is slow) → <../distributed_storage_and_tiny_rook_ceph.md> (Forgejo on a
faster/other filesystem; the RWX two-mounter share is the real penalty) → **this note**
(does a single-writer-per-repo \_HA* architecture beat the Forgejo RWX share?).

## The question

Forgejo scales by mounting one git PVC `RWX` across N replicas — that shared mount is
what tanks git push (badly on SeaweedFS FUSE; even on CephFS via MDS cap coordination).
Single-writer is fast but a single Gitaly on one node is exactly the single-point-of-
failure we're trying to avoid. GitLab's **Gitaly Cluster (Praefect)** is the topology
that is supposed to give both: 3 Gitaly nodes, every repo replicated across them with a
single writer _per repo_ (no shared mount, no cap-coordination) and **async** replication
(the primary acks the push; secondaries catch up in the background), plus automatic
failover. So it should be HA _and_ fast.

## Setup

- GitLab chart `9.11.8` (GitLab v18.11.7), helm-deployed (throwaway; see <README.md>).
- 3 stateless webservice replicas + **Praefect** (`global.praefect.enabled`, 3 Gitaly
  nodes, `replaceInternalGitaly`) — see <praefect-overlay.yaml>.
- Only Gitaly's storage class varies per cell; PG/Redis/MinIO stay on `local-path-ovh-ssd`.
- Same four operations / 20 samples as the Forgejo bench, hitting the webservice ClusterIP.

## Results — GitLab + Gitaly Cluster (Praefect, 3-node HA)

Median seconds, lower is better. Raw CSVs in <results/praefect_3node/>.

| Storage cell    | version | contents read | contents write | git push |
| --------------- | ------: | ------------: | -------------: | -------: |
| seaweedfs-SSD   |   0.044 |         0.099 |          0.885 |    1.155 |
| rook-cephfs-SSD |   0.053 |         0.100 |          0.702 |    0.790 |
| seaweedfs-HDD   |   0.046 |         0.099 |          0.870 |    0.970 |
| rook-cephfs-HDD |   0.058 |         0.082 |          1.897 |    1.490 |

The rook-HDD cell required a full sequential Ceph rebuild onto the HDD hosts (3-copy);
recovering from the force-teardown of the SSD arm was fiddly — see the rebuild playbook
and its rook-state gotchas in <README.md>.

### Anchor: same workload on the two _multi-node_ Forgejo/GitLab HA shapes

Both rows below are "no single-node dependency" topologies, on `seaweedfs-ssd`:

| Topology                         | version | contents read | contents write | git push |
| -------------------------------- | ------: | ------------: | -------------: | -------: |
| GitLab Gitaly Cluster (Praefect) |   0.044 |         0.099 |          0.885 |    1.155 |
| Forgejo 2-replica RWX            |   0.349 |         0.499 |          3.356 |    1.630 |

For reference, a **single** Gitaly (RWO, SPOF — not HA) on seaweedfs-ssd pushed at 1.010s
(<results/single_gitaly_baseline/>): Praefect's 3-way async replication adds only ~14%
over that while providing HA.

## Findings

1. **Gitaly Cluster wins HA-to-HA, on every operation.** vs Forgejo 2-replica RWX on the
   same seaweedfs-ssd: git push 1.4x, contents write 3.8x, contents read 5x, version 8x
   faster. rook-cephfs-SSD pushes at 0.79s — 2.1x faster than Forgejo-RWX. Both topologies
   avoid a single-node dependency; Praefect is simply a better way to get there.

2. **Under Praefect, the storage backend has a modest effect on git push** — the full 2x2
   spans 0.79s (rook-ssd) to 1.49s (rook-hdd), ~1.9x. The 3-copy loop-backed HDD Ceph is the
   slowest cell (write 1.9s, push 1.5s), consistent with the Forgejo trial's HDD arm. But
   even that worst cell (1.49s) still beats Forgejo 2-replica RWX (1.63s), and all four cells
   sit below it. So the RWX-vs-single-writer _architecture_ dominates the media/replication
   choice: with single-writer-per-repo + async replication, push isn't gated on the shared-
   filesystem metadata round trips that dominated the Forgejo RWX share. Version/read floors
   (~0.05 / ~0.10s) are storage-independent, as expected.

3. **The RWX shared mount was the villain, not the disk — now confirmed from both sides.**
   The rook trial showed Forgejo is slow even on fast storage (2-mounter RWX cap
   coordination); this bench shows Gitaly Cluster is fast even on slow storage (no shared
   mount). Replication moved to the application layer (per-repo, async) is both faster and
   genuinely HA.

## Takeaway for the forge

If git-write latency and HA both matter, the lever is the **storage architecture, not the
storage class**: replace the multi-replica RWX share with single-writer-per-repo +
app-layer replication. GitLab/Gitaly Cluster does this out of the box; an equivalent for
Forgejo would be single-writer instances with scheduled mirror/bundle replication (see
<../forgejo_git_write_latency.md> recommendation #1). This is a "worth building properly
and re-measuring on real raw devices" result, not a drop-in migration decision — the Ceph
OSDs here are loop-backed trial mechanisms.
