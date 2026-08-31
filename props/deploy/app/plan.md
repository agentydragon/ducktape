# Props cluster deployment — TODOs

## Langfuse

- [ ] **TODO(tracing): Langfuse traces for every active API shape** — keep one
      live smoke per active shape when changing routing.

- [ ] **TODO(reliability): ClickHouse replication** — Currently single-node on
      OVH `local-path-ovh`. For production use, deploy a replicated ClickHouse
      cluster. See `cluster/k8s/langfuse/app/helmrelease.yaml`.

- [ ] **TODO(reliability): ClickHouse backup** — PostgreSQL is CNPG OVH-HA,
      Redis is operator-managed Valkey, and blob storage is SeaweedFS S3. The
      remaining non-HA data plane is ClickHouse; add an explicit backup/export
      path before treating Langfuse traces as durable history.
