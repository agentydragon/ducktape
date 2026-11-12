# Props cluster deployment — TODOs

## Langfuse

- [ ] **TODO(tracing): Langfuse traces for every active API shape** — keep one
      live smoke per active shape when changing routing.

- [ ] **TODO(reliability): ClickHouse backup** — Langfuse uses the shared
      replicated ClickHouse cluster (`cluster/k8s/clickhouse/cluster/README.md`).
      Add an explicit backup/export and restore path before treating Langfuse
      traces as durable history; replication alone does not provide a backup.
