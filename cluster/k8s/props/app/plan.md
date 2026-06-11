# Props cluster deployment — TODOs

## Langfuse

- [ ] **TODO(tracing): LiteLLM Responses API logging** — `props/` speaks the
      OpenAI Responses API through cluster LiteLLM, but LiteLLM
      `1.86.3` does not emit Langfuse OTEL traces for the z.ai
      Responses-to-chat bridge path. Checked upstream `1.87.1` and
      `1.88.0-rc.3`; same code shape, not expected fixed. See
      `cluster/debug/2026-06-05-litellm-responses-langfuse-otel.md`.
      One possible mitigation is first-class props Chat Completions support; see
      `props/docs/plans/chat_completions_api.md`.

- [ ] **TODO(reliability): ClickHouse replication** — Currently single-node on
      OVH `local-path-ovh`. For production use, deploy a replicated ClickHouse
      cluster. See `cluster/k8s/langfuse/app/helmrelease.yaml`.

- [ ] **TODO(reliability): ClickHouse backup** — PostgreSQL is CNPG OVH-HA,
      Redis is operator-managed Valkey, and blob storage is SeaweedFS S3. The
      remaining non-HA data plane is ClickHouse; add an explicit backup/export
      path before treating Langfuse traces as durable history.
