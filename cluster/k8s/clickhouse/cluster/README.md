# Central ClickHouse

The Kubernetes resources and operator live in the `clickhouse` namespace. One
shard with two replicated ClickHouse servers and a three-member Keeper quorum,
all on the OVH HDD data tier. The cluster is intentionally independent of
Langfuse's chart-owned ClickHouse so applications can migrate one at a time.

The first tenant is `aiquota`:

- `aiquota.raw_http_observations` preserves bounded exact upstream response
  bytes (base64 + SHA-256) alongside normalized JSON.
- `aiquota.aiquota_windows` provides typed quota-window history for Grafana.
- typed quota windows retain five years of hot/queryable data;
- raw response bodies retain one year in ClickHouse, with the exact bounded
  bytes and integrity metadata available for inspection;
- a ClickHouse materialized view projects the raw row's quota-window array into
  the typed table, so each collector snapshot is one atomic insert rather than
  two independently retried writes.

`replicasUseFQDN: "yes"` makes the operator generate per-replica host entries
that resolve to each Pod's actual address. The cluster intentionally does not
set `secure: "true"`: that operator setting generates secure native-port 9440
members, which requires ClickHouse TLS listener and certificate configuration.
Without that listener, those members cannot be local DDLWorker targets. The
internal native cluster therefore consistently uses port 9000; TLS must be
configured end-to-end before enabling the operator's secure-cluster setting.

**Gotcha: `/etc/clickhouse-server/config.d` belongs to the operator.** It mounts its own
generated ConfigMap at that path, over-mounting whatever the `podTemplate` declares
there, so a `configMapGenerator` volume is silently invisible to the server — a
`system_logs.xml` wired up that way never reached the pod and the tables it disabled kept
growing for months. Server config additions go in `spec.configuration.files`, whose keys
the operator renders into that same generated ConfigMap. Files merge in sorted order, and
the operator's own `01-clickhouse-0*.xml` set `replace="1"` on `query_log`, `part_log` and
`trace_log`, so anything overriding those must sort after them.

The versioned `clickhouse-aiquota-schema-v7` Job applies idempotent tenant DDL
once through the cluster Service with `ON CLUSTER default`. Tables remain
ReplicatedMergeTree tables using the same Keeper paths. Tenant schema belongs
only to these Jobs: ClickHouse startup scripts do not create application
tables. Completed Jobs are retained as rollout evidence; a later schema
revision needs a new immutable Job name because Kubernetes cannot mutate a
completed Job template.

Each tenant gets its own database, least-privilege users, credentials, quotas,
and query profile. The schema Job creates the empty `langfuse` database; the
Langfuse HelmRelease then runs Langfuse's own versioned table migrations with
the dedicated `langfuse` user.

Applications receive separate insert-only credentials; Grafana receives a
read-only account. NetworkPolicy admits only those named consumers plus
same-namespace administration and Alloy metrics scraping.

`public_coder_analytics` is a dedicated native ClickHouse reader for the
Haku Console-managed public-coder agent. It can `SELECT` only the normalized
`aiquota.aiquota_windows` and raw `aiquota.raw_http_observations` tables, under
the shared bounded `readonly` profile and quota. The agent holds only an
Iron-proxy placeholder; the real password is reflected exclusively into its
Iron proxy, which is the sole cross-namespace Pod permitted to reach this
ClusterIP service on HTTP port 8123. ClickHouse is not exposed through an
HTTPRoute.
