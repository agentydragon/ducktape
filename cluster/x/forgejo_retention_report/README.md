# Forgejo retention report

`//cluster/x/forgejo_retention_report` produces Markdown and JSON reports;
it has no deletion or cleanup-rule installation mode.

Run locally through Bazel with the cluster kubeconfig loaded. The caller needs
cluster-wide workload/source reads and `pods/exec` on a Forgejo CNPG pod. The
database connection uses local PostgreSQL peer authentication; no registry or
database password is read or written.

```bash
bb run //cluster/x/forgejo_retention_report -- \
  --db-pod forgejo-db-ssd-1 \
  --repo /path/to/ducktape=origin/devel \
  --repo /path/to/gaffer-private=origin/main \
  --repo /path/to/finance-agent=origin/main \
  --repo /path/to/haku-state=origin/main \
  --output /tmp/forgejo-retention-report
```

Fetch sources in isolated clones first. Include both desired branch heads and
Flux's applied artifact revisions with repeated `--repo PATH=REV` arguments when
they differ. The report inventories live Flux Git sources and marks which exact
revisions were scanned; upstream sources are not cloned automatically. Uncommitted
working-tree changes are excluded. Complete rollback history requires full clones.

Defaults model deletion of `ducktape-ci` container tags matching dated `devel-*`
versions, while keeping the newest 10 versions per image, all versions under
30 days old, and references found in source, 30 days of source history, or live
workloads. Override these with `--owner`, `--keep-newest`, `--min-age-days`, and
`--rollback-days`. These parameters only affect the report.

The scanner protects dated tags and digests conservatively wherever they appear,
including split Kustomize fields, docs, tests, archived manifests, and removed
lines in rollback history. Other explicit registry references protect their exact
image/version. Old ReplicaSets, Jobs, CronJobs, VM and sandbox templates contribute
live references. External consumers and additional custom resource kinds still
need an inventory before deletion could be considered.

Package accounting reads Forgejo's `package*` tables in a repeatable-read,
read-only transaction with a 30-second statement timeout. A schema change fails
the collection. Queries read package metadata, not S3 object contents or a bucket
listing. This is a full metadata inventory, so its cost grows with package files.

A blob counts toward the candidate total only when **every** package-file
reference belongs to a candidate version, across all owners and package types.
Internal records, uploads, digest-named versions, and other owners retain their
references. Per-image totals show bytes released by removing just that image's
candidates; blobs shared across candidate images contribute only to the global
total. Already-unreferenced blobs are reported separately.

Byte counts represent logical package blobs. They exclude Actions artifacts/logs,
LFS, attachments, replication, and deleted data awaiting SeaweedFS vacuum. Source,
registry, and workload snapshots are taken separately. A candidate list is an
investigation result, not a validated deletion plan.
