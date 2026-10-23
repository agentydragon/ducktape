# SeaweedFS Forgejo retry 2

Attempt 2 reused the shared retry harness with `PIRAEUS_BENCH_ATTEMPT=2`.
That selector constrained the namespace to
`piraeus-storage-bench-20261019-seaweed-retry2`, owner label to
`seaweedfs-hdd-retry-2`, private data to
`/tmp/piraeus-storage-seaweed-retry2-private`, and outputs to this directory
and, after pooling, [`forgejo-seaweedfs-ovh.csv`](../forgejo-seaweedfs-ovh.csv)
with `attempt=2`.
The fixture, Forgejo values and client Job were copied privately with only the
namespace/owner references changed. The Forgejo chart was 17.1.1 and the app
image was 15.0.8-rootless; the workload script, image digests, storage classes,
placements and configuration matched attempt 1.

From the repository root, the stages were run as:

```sh
direnv exec . env PIRAEUS_BENCH_ATTEMPT=2 sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/prepare.sh
direnv exec . env PIRAEUS_BENCH_ATTEMPT=2 sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/run.sh
direnv exec . env PIRAEUS_BENCH_ATTEMPT=2 sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/cleanup.sh
```

The fresh 1 GiB SeaweedFS claim mounted Forgejo's `/data`; a single Forgejo app
and a separate client ran on `ovh-ns102453`. PostgreSQL 18.1 used a fresh 5 GiB
`local-path-ovh-ssd` claim and Valkey 9.1.2 was ephemeral, both pinned to
`ovh-ns104952`. The same pinned Forgejo 15.0.8 chart/image and original
`forgejo-bench.sh` workload ran: 20 samples each for version, contents read,
contents write and tiny Git push.

All 80 samples returned HTTP 2xx (60 status 200 and 20 status 201). Medians in
seconds were 0.406925 for version, 0.494261 for contents read, 2.447776 for
contents write and 1.715 for tiny Git push. The Seaweed class is media-blind,
but the fresh PVC collection was observed only on HDD disks before and after
the run; placement rows show `ReplicaPlacement:001` across the Seaweed volume
servers. This is one workload run, not a durability or failure test.

Configuration Secret events triggered Stakater Reloader updates to the
Forgejo Deployment. Its `Recreate` strategy replaced Pending Pods before the
accepted Pod became Ready. The accepted Pod was the only Ready Forgejo Pod at
measurement time; its init containers had completed, its restart count was
zero, and its spec referenced `repo-seaweedfs`. Scoped rollout events,
ReplicaSet revisions and attachment evidence are in
[`pod-replacement.txt`](pod-replacement.txt). The retained events show the
successful attach for the accepted Pod and no successful attach for the
discarded Pods; absence of an attach event alone does not establish that no
lower-level mount activity occurred. No benchmark samples started until the
accepted Pod was Ready.

Cleanup removed the app, client, database, Valkey, namespace and both PVCs
through normal Kubernetes/CSI deletion. The recorded two PVs and their
attachments are absent, the PVC collection has no remaining Seaweed volume
rows, both benchmark nodes remain Ready, all ten checked Seaweed provider Pods
are Ready, and the shared StorageClass UID is unchanged. The temporary private
credentials and complete global `volume.list` snapshots were removed.
