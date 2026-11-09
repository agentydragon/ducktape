# LiteLLM → Langfuse span scope experiment

Compares LiteLLM trace-export configs for #15038 (store each request once in Langfuse, for every
API) in a throwaway namespace. Background: `cluster/cdk8s/litellm/docs/langfuse_tracing.md`.

The namespace holds:

- an isolated Langfuse (chart `2.1.6`, image `4.67.0`), with Postgres, Valkey, ClickHouse and
  SeaweedFS S3 on `emptyDir`
- one scratch LiteLLM running the production image

The scratch LiteLLM serves one model per API shape and forwards each to the production LiteLLM
with the `cheap-experiments` key:

| Model            | Production route                        |
| ---------------- | --------------------------------------- |
| `luna-responses` | `chatgpt-sub/oai-responses/gpt-6-luna`  |
| `luna-messages`  | `chatgpt-sub/ant-messages/gpt-6-luna`   |
| `ministral-chat` | `mistral/oai-chat/ministral-14b-latest` |

Probe calls therefore also show up as ordinary traces of the production LiteLLM.

## Procedure

```bash
./setup.sh               # namespace, deps, generated secrets, copied key + pull secret, Langfuse
./run.sh <variant>       # variants/<variant>/{config.yaml,env} → scratch LiteLLM
./probe.sh <label>       # one request per API shape; per-span report from the scratch ClickHouse
kubectl delete namespace scratch-langfuse-tracing   # teardown; nothing persists
```

`probe.sh` sends attribution the way Agentplane's LLM ingress does
(`x-litellm-spend-logs-metadata` plus `litellm.metadata.spend_logs_metadata.agentplane.*` baggage),
and props' `trace_metadata` in the body.

## Variants

- **`baseline`:** production wiring (generic `otel` callback, `langfuse` mapper, `span_only` capture).
- **`llm-only`:** `langfuse_otel` callback with `LITELLM_OTEL_LANGFUSE_SPAN_SCOPE=llm_only`, plus
  the inert `EXPORTERS` entry that keeps the preset from adding a console exporter (#5757).
- **`otel-owned`:** generic `otel` callback with `llm_only`. The Langfuse exporter is declared in
  `EXPORTERS` with `"owner": "langfuse_otel"`, so the scope applies without the preset.

## Results (2026-11-09, LiteLLM 1.103.0)

| Variant      | Spans per request          | Input copies | Root has input/output | Agentplane attribution | Props `trace_metadata` | Content in pod logs | Exporter errors                    |
| ------------ | -------------------------- | ------------ | --------------------- | ---------------------- | ---------------------- | ------------------- | ---------------------------------- |
| `baseline`   | 3 (`POST`, `auth`, `chat`) | 2            | yes                   | on every span          | absent                 | none                | none                               |
| `llm-only`   | 1 (`chat`)                 | 1            | yes                   | yes                    | absent                 | none                | tracebacks from the inert exporter |
| `otel-owned` | 1 (`chat`)                 | 1            | yes                   | yes                    | absent                 | none                | none                               |

The same results held for `/v1/responses`, `/v1/messages` and `/v1/chat/completions`. Under both
scoped variants the model-call span is the trace root and supplies the trace name
(`chat <model>`).

- **`otel-owned` meets #15038's acceptance** without the preset or a failing placeholder exporter.
- **No variant carries props' `trace_metadata`,** including `langfuse_otel`.
- **Not covered:** streaming requests and rejected requests.
