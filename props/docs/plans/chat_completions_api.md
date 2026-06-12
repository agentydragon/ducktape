# Plan: Chat Completions support in props LLM proxy

**Status:** first implementation is complete on the chat-completions branch.
Deployment and end-to-end cluster verification are still pending.

## Motivation

Props already exposes an OpenAI-compatible `/v1/responses` endpoint to agent
containers. That works well for Responses-native clients, but it forces
chat-only providers such as z.ai through LiteLLM's Responses-to-chat translation
path.

That translation path has been operationally annoying: LiteLLM `1.86.3` did not
emit Langfuse OTEL traces for z.ai calls made through `/v1/responses`, and
upstream `1.87.1` / `1.88.0-rc.3` showed the same relevant code shape during
inspection. See `cluster/debug/2026-06-05-litellm-responses-langfuse-otel.md`.

The goal is to let props speak the OpenAI Chat Completions API directly when a
model/provider is naturally chat-shaped, while preserving the existing
Responses path for agents and models that already use it.

## Non-goals

- Do not make Chat Completions payloads pretend to be `ResponsesRequest` or
  `ResponsesResult`.
- Do not build a full chat transcript UI in the first slice. A raw JSON fallback
  is enough as long as the backend/API no longer rejects chat-shaped rows.
- Do not replace the existing `/v1/responses` path or force all agents to move.
- Do not rely on LiteLLM's Responses-to-chat bridge for tracing correctness.

## Completed First Slice

The first implementation keeps Responses and Chat Completions as distinct wire
shapes and records which shape was used:

```text
api_shape = "responses" | "chat_completions"
```

Completed pieces:

- Added `api_shape` to model metadata/config so each logical model declares
  exactly one supported API shape.
- Added `api_shape` to `llm_requests` so each logged request records the actual
  API shape used.
- Added `/v1/chat/completions` to the props LLM proxy.
- Kept `/v1/responses` behavior stable.
- Enforced endpoint/model shape compatibility:
  - `/v1/responses` requires `api_shape = "responses"`.
  - `/v1/chat/completions` requires `api_shape = "chat_completions"`.
- Mapped Chat Completions usage into the existing cost columns:
  - `usage.prompt_tokens` -> `input_tokens`
  - `usage.prompt_tokens_details.cached_tokens` -> `cached_input_tokens`
  - `usage.completion_tokens` -> `output_tokens`
- Kept cost/budget accounting on the existing token columns.
- Added metadata injection for correlation through LiteLLM/Langfuse:

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

- Updated `GET /api/runs/{id}/llm_requests` to treat request/response bodies as
  raw JSON audit payloads instead of forcing all rows through Responses models.
- Kept the existing frontend Responses renderer and added a raw JSON fallback
  for chat-shaped request rows.
- Added an agent model-adapter boundary:
  - Responses-backed models use the existing Responses client path.
  - Chat-backed models use `client.chat.completions.create(...)`.
  - Internal transcript/request objects stay neutral enough for the core loop.
- Used OpenAI SDK chat parameter/usage types where they describe the chat API
  boundary. Raw persisted/proxied JSON remains `dict[str, Any]`.
- Configured `glm-4.6` as a chat-completions model in props config.

## z.ai Findings

Live z.ai testing against the coding endpoint confirmed the basic chat adapter
path works with `glm-4.6`:

- OpenAI-compatible Chat Completions requests work.
- Function `tools` work.
- `tool_choice: "auto"` works.
- Omitting `tool_choice` works.
- Strict tool metadata works: `function.strict: true`, `required`, `enum`, and
  `additionalProperties: false`.
- Both `max_completion_tokens` and legacy `max_tokens` are accepted.

z.ai does **not** accept forced named OpenAI tool-choice objects:

```json
{ "type": "function", "function": { "name": "record_result" } }
```

That returns `400` / code `1210`. If z.ai needs to call a specific tool, use
prompt instructions plus `tool_choice: "auto"` unless/until props gains a
provider capability flag for this quirk. The detailed API notes live in
`docs/zai_api.md`.

## Remaining Deployment Work

1. Merge the chat-completions branch.
2. Deploy props proxy/backend.
3. Sync/update model metadata so `glm-4.6` is `api_shape = "chat_completions"`.
4. Smoke test `/v1/responses` on an existing Responses model to prove no
   regression.
5. Smoke test `/v1/chat/completions` with `glm-4.6` through cluster LiteLLM.
6. Verify a matching `llm_requests` row exists with
   `api_shape = "chat_completions"`.
7. Verify Langfuse receives a trace for the native chat call.
8. Verify props metadata is visible in the Langfuse trace/generation metadata.
9. Run a critic/grader path against `glm-4.6` through props/LiteLLM and confirm
   tool calls, request logging, budget accounting, and Langfuse metadata all
   line up.

## Remaining Design Questions

- Should props log the client request, the forwarded upstream request, or both?
  Current behavior logs the forwarded request after model rewrite/metadata
  injection.
- Do we want a first-class `correlation_id` column now, or is metadata-only
  correlation enough until we need direct DB-to-Langfuse joins?
- Do we need a provider/model capability for "no forced named chat tool choice"
  so z.ai can avoid `ToolChoiceFunction` while OpenAI-compatible providers that
  support it keep using it?
- Should a future `ResponsesViaChatModel` adapter exist for Responses-based
  agents that must run against chat-only providers? If so, keep it at the model
  client boundary; do not make the DB/API pretend chat payloads are Responses
  payloads.

## Later Frontend Work

The first slice intentionally avoids a specialized chat transcript UI. If there
is demand, add a chat renderer for:

- `messages`
- `choices`
- `message.tool_calls`
- `usage`
- provider-specific fields such as `reasoning_content`

Keep the raw JSON fallback even after a richer renderer exists; provider
extensions are common and useful during debugging.

## Verification So Far

Completed verification on the branch:

```bash
bbr test //agent_core/... //openai_utils/... //props/backend/routes/... //props/llm_proxy/... --test_tag_filters=-live_openai_api
bazelisk test //agent_core:test_zai_chat_adapter_live --test_output=streamed --nocache_test_results --test_env=ZAI_API_KEY --test_env=ZAI_MODEL
bbr test //props/frontend:svelte_check_test //props/frontend/src/components:visual_LLMRequests //props/frontend/src/components:visual_LLMRequestsToolCall
bbr test //props/backend/routes/... //props/llm_proxy:test_app --test_tag_filters=-live_openai_api
bbr build //props/backend/routes:model_metadata
bbr test //props/frontend:svelte_check_test
bbr test //props/llm_proxy:test_routing
```
