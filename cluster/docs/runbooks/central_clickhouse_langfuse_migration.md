# Central ClickHouse and Langfuse migration

This change replaces the old analytics ClickHouse installation with the
central `clickhouse` installation in the `clickhouse` namespace. It also
switches Langfuse from its chart-owned ClickHouse to the central service. The
chart-owned ClickHouse remains deployed temporarily during the copy so it can
serve as the source; a follow-up change disables it after the rollback window.

The central installation is one shard with two ClickHouse replicas and three
Keeper replicas. The replicas share data through ClickHouse replication, but
their PVCs are node-local. A node failure is therefore tolerated while the
other replica and Keeper quorum remain available; permanent loss of a node's
local disk still requires restore or manual rebuild.

## Before reconciliation

Do not reconcile the final Git revision until the disposable AIQuota data has
been exported. Keep the dump outside the repository and protect it because
raw observations contain captured upstream response bytes.

Use the existing administrative ClickHouse access to export the source raw
table in Native format:

```bash
clickhouse-client --query \
  'SELECT * FROM aiquota.raw_http_observations FORMAT Native' \
  > /secure/path/aiquota.raw_http_observations.native
```

The typed AIQuota tables are materialized views of that raw table. Restoring
the raw rows into the new installation repopulates the derived tables through
the same views. Record row counts and a checksum query on the source for
post-restore comparison.

Langfuse's source ClickHouse must also be backed up before switching the
HelmRelease. Keep the current Langfuse version unchanged, stop Langfuse
ingestion for the final copy, and copy only the `langfuse` data with a
ClickHouse-supported ClickHouse-to-ClickHouse or backup/restore procedure.
On the current bundled chart, the source database is `default`; the target
configuration uses the isolated `langfuse` database. Enumerate the source
tables before copying and map `default.<table>` to `langfuse.<table>`.
Do not copy the `system` database or Keeper metadata. The target table
definitions must come from Langfuse's own migrations so they are created as
replicated tables on the new cluster; copy rows into those target tables
rather than restoring the source table engines over them. Preserve and
compare the Langfuse schema-migrations state. Validate source and target row
counts before the cutover.

## Rebuild and restore

1. Take the two backups above and record their checksums and source row counts.
2. Reconcile the central ClickHouse resources. The `clickhouse-namespace`
   Kustomization creates the namespace, the operator watches it, and the
   schema Job creates both `aiquota` and the empty `langfuse` database.
3. Wait for the `clickhouse` Flux Kustomization, both ClickHouse replicas,
   all three Keeper members, and `clickhouse-schema` to become Ready.
4. Restore the AIQuota Native dump through the new
   `clickhouse.clickhouse.svc.cluster.local:9000` endpoint and compare the
   recorded counts/checksums. The AIQuota application can remain paused until
   this check passes.
5. Enter the Langfuse maintenance window. Stop the Langfuse web/worker and
   AIQuota writer, but leave the old chart-owned ClickHouse running. Reconcile
   the external ClickHouse configuration; the chart temporarily keeps its old
   ClickHouse deployment alive as the source while the application points at
   the central service. Let Langfuse's automatic migrations create the target
   table definitions.
6. Copy the old Langfuse rows from
   `langfuse-clickhouse.langfuse.svc.cluster.local:9000` into the target tables
   over the native protocol and validate the main trace/observation tables
   plus `schema_migrations`. The central NetworkPolicy temporarily admits only
   the old Langfuse ClickHouse pod for this native copy.
7. Resume Langfuse. HTTP traffic uses port 8123, migrations use port 9000,
   and automatic migrations remain enabled because the logical cluster name is
   `default`. Verify login, trace ingestion, trace lookup, and background
   worker activity. Verify AIQuota ingestion and the Grafana dashboard
   separately.
8. After the rollback window, set the Langfuse chart's ClickHouse deployment
   to `false` in a follow-up change, remove the old source resources/PVCs, and
   delete the temporary `analytics` watch entry from the operator HelmRelease.
   Remove the local dump after the restore is accepted.

The old Langfuse chart-owned PVC is kept as a source only during the copy and
should not be treated as the durable rollback artifact. Changing
`clickhouse.deploy` to `false` removes the chart-managed workload. The
independent backup is the rollback artifact; rollback to the old HelmRelease
values only while that backup/source data is still available.
