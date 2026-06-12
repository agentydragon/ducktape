# Props cluster deployment — TODOs

## Langfuse

- [ ] **TODO(tracing): Langfuse traces for every active API shape** — the old
      z.ai Responses-to-chat bridge did not emit LiteLLM Langfuse OTEL traces
      (`cluster/debug/2026-06-05-litellm-responses-langfuse-otel.md`).
      Props now supports native Chat Completions and Anthropic Messages proxy
      shapes; the current GLM models in `config.toml` use Anthropic because
      z.ai's Chat Completions tool-call parser mishandles union-shaped tool
      parameters. Keep one live smoke per active shape when changing routing.

- [ ] **TODO(reliability): ClickHouse replication** — Currently single-node on
      OVH `local-path-ovh`. For production use, deploy a replicated ClickHouse
      cluster. See `cluster/k8s/langfuse/app/helmrelease.yaml`.

- [ ] **TODO(reliability): ClickHouse backup** — PostgreSQL is CNPG OVH-HA,
      Redis is operator-managed Valkey, and blob storage is SeaweedFS S3. The
      remaining non-HA data plane is ClickHouse; add an explicit backup/export
      path before treating Langfuse traces as durable history.
