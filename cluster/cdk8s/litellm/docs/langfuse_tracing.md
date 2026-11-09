# Langfuse tracing

LiteLLM exports OpenTelemetry spans to the in-cluster Langfuse (`langfuse-litellm-project`).
Pinned versions: LiteLLM `1.103.0`, Langfuse `4.67.0`.

## Wiring

- `config.py`: `callbacks: ["otel", "prometheus"]`, with `callback_settings.otel` holding
  `mapper_names: ["langfuse"]` and the `baggage_metadata_keys` that carry Agentplane
  attribution onto every span.
- `proxy.py`: `LITELLM_OTEL_V2=true`,
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only`.
- `secrets.py`: ESO builds `litellm-langfuse-otel-config` from `langfuse/langfuse-secrets`.
  It supplies `OTEL_EXPORTER_OTLP_ENDPOINT` (`/api/public/otel`) and
  `OTEL_EXPORTER_OTLP_HEADERS` (Basic auth plus `x-langfuse-ingestion-version=4`).
- Both env vars are optional, so the proxy starts without Langfuse. In that case LiteLLM
  falls back to its console exporter.

## Gotcha: the `langfuse_otel` callback writes prompts to pod logs

We use the generic `otel` callback rather than `langfuse_otel` for this reason (#11937,
switched in #11951).

- LiteLLM's v2 config defaults `exporter` to `"console"`.
- The `langfuse_otel` preset appends its OTLP exporter to that default instead of
  replacing it.
- With content capture on, every span therefore also goes to stdout, and from there
  to Loki.

If `langfuse_otel` is needed again, set `EXPORTERS` to a JSON list holding one inert
`otlp_http` entry. A non-empty list stops the console default from being constructed;
this was verified in #5757.

## Gotcha: Responses-to-chat bridge routes may not trace

A route with `use_chat_completions_api = true` serves `/v1/responses` through LiteLLM's
Responses-to-chat bridge. On `1.86.3` such requests (z.ai `glm-4.6`) returned 200 but
wrote no Langfuse trace:

- The bridge returns before the native Responses logging update.
- The inner chat call consumes the OTel emit-once guard.

No current route uses the bridge. Retest with a unique marker in the request if one is
added.

## Content capture and its storage cost

`span_only` exists so the `/v1/responses` root observation has input and output
(#5761, #11608). It stores prompts in a way that grows fast:

- **Each request's input is stored twice.** It lands on both the proxy's
  `POST /v1/...` HTTP span and the `chat <model>` span. On 2026-10-29, 3387 of 3391
  traces had input on both, and 2911 were byte-identical.
- **Agent loops store their history quadratically.** Every turn re-sends, and Langfuse
  re-stores, the whole context. Sessions of ~245 turns stored ~140× their final context.
- **Neither Langfuse nor ClickHouse recovers the repetition.** Langfuse has no prefix
  dedup. ClickHouse compression (`ZSTD(3)`, ~3.5×) can't either, because each turn is a
  separate trace and rows are sorted by a trace-id hash, so turns never share a
  compression block.
- **Content-free spans also arrive**, about 22k/day: `auth /metrics`, `postgres *` and
  `reset_budget_job`.
- **Before 2026-10-24, the v1 exporter stored more copies.** It emitted a
  `raw_gen_ai_request` span holding the request again as `llm.invocation_parameters`,
  `llm.None.tools` and `llm.None.messages`. Langfuse copied those into
  `metadata.attributes` at about 900 KiB per row. That data is still in `events_full`.

### Levers

- `LITELLM_OTEL_LANGFUSE_SPAN_SCOPE=llm_only` exports only the model-call spans, with
  the model call as the trace root.
  - It applies only to the exporter owned by `langfuse_otel`
    (`integrations/otel/plumbing/providers.py`). On our generic `otel` exporter it
    does nothing.
  - It is in the source and the admin UI's callback config, but not in LiteLLM's docs.
  - Using it means switching back to `langfuse_otel` with the `EXPORTERS` workaround.
- `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content` drops message content.
  The GenAI mapper still emits tool schemas (`gen_ai.tool.*`), so those keep arriving.

## Langfuse-side retention

- **Data retention:** Langfuse's built-in data retention is Enterprise-only when
  self-hosted. The OSS alternative is a ClickHouse TTL you add yourself.
- **Legacy tables:** v4 writes only `events_full`/`events_core`. `cluster/cdk8s/langfuse.py`
  pins `LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only`, and the legacy `traces`/`observations`
  tables were truncated on 2026-11-08.
- **Raw S3 payloads:** the raw ingestion payloads in S3 never expire: #15022.
  Freed thin-pool space needs a trim: #15021.

## Open: props correlation keys

`props/llm_proxy/routes.py` sends correlation keys as LiteLLM `trace_metadata`. In
`1.103.0`, only the v1 `integrations/langfuse/langfuse_otel.py` logger reads
`trace_metadata`. The v2 `langfuse` mapper our `otel` callback uses does not, so these
keys may not reach Langfuse. This has not been verified against a live props request.
