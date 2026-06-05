# Plan: Chat Completions support in props LLM proxy

**Status:** implementation in progress.

## Motivation

Props currently exposes an OpenAI-compatible `/v1/responses` endpoint to agent
containers. That works well for OpenAI Responses-native clients, but it forces
chat-only providers such as z.ai through LiteLLM's Responses-to-chat translation
path.

That translation path has already been operationally annoying: LiteLLM `1.86.3`
does not emit Langfuse OTEL traces for z.ai calls made through `/v1/responses`,
and upstream `1.87.1` / `1.88.0-rc.3` showed the same relevant code shape during
inspection. See `cluster/debug/2026-06-05-litellm-responses-langfuse-otel.md`.

The goal is to let props speak the OpenAI Chat Completions API directly when a
model/provider is naturally chat-shaped, while preserving the existing Responses
path for agents and models that already use it.

## Non-goals

- Do not make Chat Completions payloads pretend to be `ResponsesRequest` or
  `ResponsesResult`.
- Do not build a full chat transcript UI in the first slice. A raw JSON fallback
  is enough as long as the backend/API no longer rejects chat-shaped rows.
- Do not replace the existing `/v1/responses` path or force all agents to move.
- Do not rely on LiteLLM's Responses-to-chat bridge for tracing correctness.

## Current state

- `props/backend/routes/llm.py` served only `POST /v1/responses` before this
  implementation.
- `llm_requests` stores raw `request_body` and `response_body` as JSONB plus
  extracted token counts: `input_tokens`, `cached_input_tokens`, `output_tokens`.
- `llm_request_costs`, `llm_run_costs`, and `agent_run_budget_status` compute
  cost/budget from those extracted token columns.
- `GET /api/runs/{id}/llm_requests` currently validates rows as
  `ResponsesRequest` / `ResponsesResult`, so chat-shaped rows would break the
  read API even though the DB can store the raw JSON.
- Model routing is already endpoint-agnostic in spirit: `model_metadata` maps the
  logical props model to `upstream_name` and `upstream_model`, and config maps the
  upstream name to a base URL and API key.
- Custom model config currently declares upstream routing, pricing, and limits,
  but not which OpenAI-compatible API shape is expected for the model.

## Target shape

The LLM proxy should support both endpoints:

- `POST /v1/responses`
- `POST /v1/chat/completions`

Both endpoints should share:

- agent credential authentication
- allowed-model enforcement
- budget preflight
- upstream routing through `model_metadata` + `upstreams`
- request/response logging to `llm_requests`
- cost accounting from token usage
- correlation metadata injection for Langfuse/searchability

The DB/API should record which API shape was used:

```text
api_shape = "responses" | "chat_completions"
```

Raw request and response payloads remain API-shape-specific JSON.

Models declare exactly one API shape:

```text
model_metadata.api_shape = "responses" | "chat_completions"
```

The model declaration is a routing/capability contract and the agent runtime uses
it to choose the model adapter. The request row's `llm_requests.api_shape` is the
historical fact of what endpoint was actually called.

## Schema changes

MVP migration:

```sql
ALTER TABLE llm_requests
  ADD COLUMN api_shape text NOT NULL DEFAULT 'responses';

ALTER TABLE llm_requests
  ADD CONSTRAINT llm_requests_api_shape_check
  CHECK (api_shape IN ('responses', 'chat_completions'));

ALTER TABLE model_metadata
  ADD COLUMN api_shape text NOT NULL DEFAULT 'responses';

ALTER TABLE model_metadata
  ADD CONSTRAINT model_metadata_api_shape_check
  CHECK (api_shape IN ('responses', 'chat_completions'));
```

SQLAlchemy:

- Add an `LLMApiShape` `StrEnum` or literal-backed string column.
- Set existing rows to `responses` through the default.
- Include `api_shape` in `LLMRequestInfo`.
- Add `api_shape` to `ModelMetadata` and `CustomModelConfig`, defaulting to
  Responses for backward compatibility.
- Sync model API-shape fields into `model_metadata` along with the existing
  pricing, limit, and upstream routing fields.

No cost-view shape change is required if chat usage is mapped into the existing
token columns. If partial usage objects are accepted, harden the view expression
to `COALESCE` each token column individually; the current expression coalesces
the final cost value, so one unexpected `NULL` term can zero the whole request's
cost.

Optional later columns, not needed for MVP:

- `upstream_name` and `upstream_model` for historical routing/debugging.
- `upstream_status_code` instead of encoding HTTP errors as text.
- `correlation_id` if we want a first-class DB key that also appears in
  Langfuse metadata.

## Token usage mapping

Responses rows keep the current extraction:

- `usage.input_tokens` -> `input_tokens`
- `usage.input_tokens_details.cached_tokens` -> `cached_input_tokens`
- `usage.output_tokens` -> `output_tokens`

Chat Completions rows map OpenAI-compatible chat usage as:

- `usage.prompt_tokens` -> `input_tokens`
- `usage.prompt_tokens_details.cached_tokens` -> `cached_input_tokens`
- `usage.completion_tokens` -> `output_tokens`

If the whole `usage` object is missing, store `NULL` token counts and compute
zero cost. If the `usage` object is present but one subfield is missing, prefer
storing `0` for the missing subfield or hardening the cost view to coalesce each
column. Do not require every OpenAI-compatible provider to emit OpenAI's full
details object.

## Backend proxy changes

1. Split the current request handling into shared helpers:
   - parse JSON
   - validate `model`
   - reject streaming for now
   - enforce model restriction
   - check budget
   - resolve upstream route
   - inject correlation metadata
   - forward to an upstream path
   - log request/response/error with `api_shape`

2. Keep `/v1/responses` behavior stable:
   - forward to `{upstream.url}/responses`
   - keep rejecting `previous_response_id`
   - keep removing `store`
   - extract Responses usage

3. Add `/v1/chat/completions`:
   - forward to `{upstream.url}/chat/completions`
   - reject `stream: true` initially, matching the Responses proxy limitation
   - rewrite `model` to `upstream.model_name`
   - extract Chat Completions usage
   - log as `api_shape='chat_completions'`

4. Enforce model API-shape capabilities.
   - `/v1/responses` requires `api_shape='responses'`.
   - `/v1/chat/completions` requires `api_shape='chat_completions'`.
   - Explicit endpoint calls still record their actual endpoint in
     `llm_requests.api_shape`.

5. Preserve the logical props model in `LLMRequest.model`.
   - This is what pricing joins and run summaries use.
   - If debugging needs the upstream model later, add explicit upstream columns
     rather than overloading `model`.

6. Consider logging the original client request body rather than the mutated
   upstream body.
   - Current code mutates `body["model"]` before logging.
   - For Langfuse correlation, the proxy can still inject metadata before
     forwarding; the DB can either store the forwarded body or store both client
     and upstream forms in a follow-up.

## Correlation and Langfuse metadata

For Chat Completions through cluster LiteLLM, LiteLLM's native chat path should
record Langfuse traces. Props should make those traces easy to connect back to
agent runs by merging metadata into the outgoing request:

```json
{
  "metadata": {
    "props.agent_run_id": "...",
    "props.api_shape": "chat_completions",
    "props.logical_model": "glm-4.6",
    "props.upstream_model": "glm-4.6"
  }
}
```

Rules:

- Preserve caller-provided `metadata` when present.
- Prefix props-owned keys with `props.` to avoid collisions.
- Do not assume `metadata.trace_id` becomes Langfuse's trace id; previous live
  chat testing showed LiteLLM keeps client trace ids in metadata/attributes rather
  than making them the ClickHouse `traces.id`.
- If exact DB-row-to-Langfuse lookup becomes important, generate a proxy
  `correlation_id` at request start, store it in the DB row, and put the same
  value in outgoing metadata.

## API and frontend changes

The first backend API change should be correctness, not presentation polish:

- Add `api_shape` to `LLMRequestInfo`.
- Stop validating all `request_body` values as `ResponsesRequest`.
- Stop validating all successful `response_body` values as `ResponsesResult`.
- Either expose raw `dict[str, Any]` bodies for all rows, or use a discriminated
  union keyed by `api_shape`.

Frontend MVP:

- Keep the existing Responses renderer for `api_shape === "responses"`.
- For `api_shape === "chat_completions"`, show the same row header plus raw JSON
  request/response sections.
- Defer a specialized chat renderer for `messages`, `choices`, `tool_calls`, and
  `usage` until there is real demand.

## Typing approach

Do not try to force Chat Completions into the existing `ResponsesRequest` /
`ResponsesResult` wrappers.

For the proxy path, prefer minimal runtime validation:

- request is valid JSON
- `model` is present and is a string
- `stream` is absent or false
- `metadata`, if present, is an object so props can merge correlation keys
- response body is JSON when the upstream returns JSON

The proxy should then extract the small typed accounting surface it actually
uses:

```python
class LLMUsageCounts(BaseModel):
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
```

Implement separate extraction functions:

```python
def extract_responses_usage(body: Mapping[str, Any] | None) -> LLMUsageCounts: ...
def extract_chat_completions_usage(body: Mapping[str, Any] | None) -> LLMUsageCounts: ...
```

For `GET /api/runs/{id}/llm_requests`, the least brittle MVP is:

```python
class LLMRequestInfo(BaseModel):
    id: int
    api_shape: LLMApiShape
    model: str
    request_body: dict[str, Any]
    response_body: dict[str, Any] | None
    response_error_body: dict[str, Any] | None
    error: str | None
    latency_ms: int | None
    created_at: datetime
```

That intentionally makes the transcript API an audit/log API rather than a
strict OpenAI schema validator. The raw payload is already persisted as JSONB,
and OpenAI-compatible providers routinely add fields that are useful to keep.

If richer typing becomes valuable later, add a discriminated union keyed by
`api_shape`:

```python
ResponsesLLMRequestInfo | ChatCompletionsLLMRequestInfo
```

Even then, keep both shapes permissive (`extra="allow"`) and avoid requiring
complete OpenAI SDK parity. The typed fields should be the ones the UI or
accounting code consumes, not every possible provider-specific extension.

For client code that actually makes model calls, prefer the OpenAI SDK's own
types where possible. `openai_utils.retry.chat_create_with_retries` already uses
`CompletionCreateParams` and returns `ChatCompletion`; that is the right layer
for typed chat-calling helpers. The DB transcript layer does not need to share
that exact type.

## Client usage

Agents that want chat completions should be able to use the normal OpenAI SDK
against the same `OPENAI_BASE_URL` and `OPENAI_API_KEY` they already receive:

```python
await client.chat.completions.create(
    model="glm-4.6",
    messages=[{"role": "user", "content": "hello"}],
)
```

This does not require converting existing Responses-based agents. It only enables
agents or helper libraries that already know how to call Chat Completions to do so
through props' auth, budget, logging, and routing layer.

For config-backed custom models, chat-shaped providers should declare the API
shape explicitly:

```toml
[[models]]
name = "glm-4.6"
upstream = "litellm"
upstream_model = "glm-4.6"
api_shape = "chat_completions"
```

If a provider supports both paths, declare the one props-controlled agents should
use for that logical model.

## Tests

Backend tests should cover:

- `/v1/chat/completions` requires agent auth.
- model restriction still applies.
- `stream: true` is rejected.
- the proxy forwards to `/chat/completions`, not `/responses`.
- logical model is rewritten to the upstream model in the forwarded body.
- chat usage is extracted into `input_tokens`, `cached_input_tokens`, and
  `output_tokens`.
- models reject endpoint calls that do not match `model_metadata.api_shape`.
- chat rows affect `llm_run_costs` and `agent_run_budget_status`.
- `GET /api/runs/{id}/llm_requests` returns both Responses and Chat Completions
  rows without validation errors.

Likely focused targets:

```bash
bbr test //props/backend/routes:test_llm_routing
bbr test //props/backend/routes:test_llm_budget
bbr test //props/backend/routes:test_runs
```

Add frontend tests only if the raw JSON fallback touches existing components in a
non-trivial way.

## Deployment slice

1. Land schema/API/backend support with no config change.
2. Deploy props proxy/backend.
3. Update `glm-4.6` config/model metadata to declare
   `api_shape='chat_completions'`.
4. Smoke test `/v1/responses` on an existing model to prove no regression.
5. Smoke test `/v1/chat/completions` with `glm-4.6` through cluster LiteLLM.
6. Verify a matching `llm_requests` row exists with
   `api_shape='chat_completions'`.
7. Verify Langfuse received a trace for the native chat call and that props
   metadata is visible in the trace/generation metadata.

## Open questions

- Should props log the client request, the forwarded upstream request, or both?
  Current code logs the forwarded request after model rewrite.
- Do we want a first-class `correlation_id` column now, or is metadata-only
  correlation enough until we need direct DB-to-Langfuse joins?
- Should a future `ResponsesViaChatModel` adapter exist for Responses-based
  agents that must run against chat-only providers? If so, keep it at the model
  client boundary; do not make the DB/API pretend chat payloads are Responses
  payloads.
