# SeaweedFS Forgejo retry

This is one isolated retry of the failed disposable SeaweedFS Forgejo cell. It
uses namespace `piraeus-storage-bench-20261019-seaweed-retry1`, a fresh 1 GiB
`seaweedfs-ovh` claim mounted at Forgejo's `/data`, one Forgejo replica and the same
`forgejo-bench.sh` stored by the parent benchmark. It creates one disposable
CNPG cluster on the fixed SSD class at `ovh-ns104952`, one fresh `bench_seaweedfs`
database, and an ephemeral Valkey instance on that node. Forgejo and its client
run on `ovh-ns102453`; only Forgejo writes the SeaweedFS claim.

The retry uses Forgejo image digest
`sha256:8f97b55ca162ef3b538c6c78a2a077df9f2143c41d80b2bc6b6920f8d430df34`,
CNPG PostgreSQL 18.1 digest
`sha256:51bc94428be3df3e234447278e33803544a93d27a57a4b6af737f218d6472d12`,
Valkey 9.1.2 digest
`sha256:a0dbf4c1d5708782907c10e2c72deff317518518b5288a58416981d9db95d30b`,
and Forgejo chart 17.1.1 OCI manifest digest
`sha256:a99352bc5167aba8b58f0de386e777e7fe742bdd573daf82f38986239ff52128`.
The pulled chart archive SHA-256 was
`26aa3120e020b67593ce6e2266c457ee925ff514f606563e184b09ed6581e18f`. The chart
is rendered privately before installation and the actual app/client Pod image
IDs, node and PVC are checked before accepting samples.

From the repository root, after confirming the namespace is absent and the
storage provider is healthy:

```sh
direnv exec . sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/prepare.sh
direnv exec . sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/run.sh
direnv exec . sh cluster/debug/piraeus_migration/benchmarks/seaweedfs_retry/cleanup.sh
```

`PIRAEUS_BENCH_ATTEMPT` defaults to 1 and accepts only 1 or 2. Setting it to
2 selects a separate namespace, private directory, evidence directory and CSV;
the exact attempt-2 command and runtime notes are in
[`results/seaweedfs-hdd-retry-2/README.md`](../results/seaweedfs-hdd-retry-2/README.md).

`prepare.sh` refuses an existing namespace or PV reference, checks the target
nodes and SeaweedFS/CSI readiness, creates private random credentials under
`/tmp/piraeus-storage-seaweed-retry1-private`, then applies only the listed
retry fixtures. It creates no Piraeus resources and changes no shared storage
configuration. `run.sh` bounds a non-ready Forgejo startup to four minutes and
stops sooner after three restarts or two minutes of stationary non-running
state. It saves status/events and CSI/FUSE log lines scoped to this PVC and Pod
if startup fails. Successful runs emit 20 samples each for version,
contents-read, contents-write and tiny Git push. After row counts and HTTP status
are checked, the 80 rows are added to the pooled results CSV with their `attempt`
number. The runner refuses an attempt already present in that CSV.

The default `seaweedfs-ovh` class is media-blind. The run therefore captures
the new PVC collection's Seaweed volume rows before and after the workload and
records the actual disk tier shown by `weed shell volume.list`; the class name
alone does not establish HDD placement. The original failed Forgejo cell had
mounted a PVC already used by the direct filesystem workload, then unstaged and
restaged it before its `DeadlineExceeded`/EIO startup failure. This retry uses a
fresh PVC's first application mount. This retry succeeded on that first mount
with no Forgejo restarts. It recorded 20 successful 2xx samples for each
operation; the scoped collection was on HDD-only Seaweed volume servers with
replica placement `001` both before and after measurement. The raw CSV is
[`forgejo-seaweedfs-ovh.csv`](../results/forgejo-seaweedfs-ovh.csv), filtered to `attempt=1`.
Medians in seconds were 0.3553805 for version, 0.4133175 for contents read,
2.289788 for contents write and 1.595 for tiny Git push. This is a measurement
on one fresh PVC and does not explain the earlier startup failure or establish
that remount behavior is fixed.

`cleanup.sh` checks the namespace owner label, stops Forgejo and its client,
drops the benchmark database while CNPG is healthy, removes the temporary
Valkey/CNPG workloads, deletes only the exact recorded SeaweedFS and database PVCs
through their normal CSI reclaim paths, and verifies the namespace, PVs and
provider health. It never operates directly on shared Seaweed buckets or
volumes. Keep the raw global Seaweed `volume.list` snapshots private; only rows
for the new PVC collection are written to committed evidence.
