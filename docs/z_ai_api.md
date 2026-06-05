# Z.ai API Shapes

Z.ai (international frontend for Zhipu/智谱) exposes GLM models through multiple
API-compatible endpoints. Canonical docs at <https://open.bigmodel.cn/dev/api>.
Auth uses a single API key (`ZAI_API_KEY`) across all endpoints.

## Available Endpoints

### 1. OpenAI Chat Completions (general)

```
POST https://api.z.ai/api/paas/v4/chat/completions
Authorization: Bearer $ZAI_API_KEY
```

Standard OpenAI `/v1/chat/completions` shape. Supports streaming (`"stream": true`),
`reasoning_content` in streaming chunks. Lists models at `/api/paas/v4/models`.

### 2. OpenAI Chat Completions (coding — GLM Coding Plan)

```
POST https://api.z.ai/api/coding/paas/v4/chat/completions
Authorization: Bearer $ZAI_API_KEY
```

Identical shape to the general endpoint but routes through the GLM Coding Plan quota.
**Must** use `https://api.z.ai/api/coding/paas/v4` instead of the general endpoint when
on a Coding plan; the general endpoint does not count against coding quota.
Lists models at `/api/coding/paas/v4/models`.

### 3. Anthropic Messages

```
POST https://api.z.ai/api/anthropic/v1/messages
x-api-key: $ZAI_API_KEY
anthropic-version: 2023-06-01
```

Anthropic `/v1/messages` compatible shape. Returns standard Anthropic response fields
(`type: "message"`, `content[].type: "text"`, `stop_reason`, `usage`).
Lists models at `/api/anthropic/v1/models`.

### 4. Async Chat Completions

```
POST https://api.z.ai/api/paas/v4/async/chat/completions
Authorization: Bearer $ZAI_API_KEY
```

Documented at `open.bigmodel.cn`. Submits a chat completion request asynchronously.
Returns a task ID; poll for results. Useful for long-running generations that exceed
synchronous timeout limits. Same request shape as synchronous Chat Completions.

### 5. Embeddings

```
POST https://api.z.ai/api/paas/v4/embeddings
Authorization: Bearer $ZAI_API_KEY
```

Endpoint exists but returns 429 ("Insufficient balance or no resource package") on the
current plan. The `open.bigmodel.cn` docs list embedding models (e.g., `embedding-3`).
Likely requires a separate embedding quota/resource pack.

## Tool Use / Function Calling

The Chat Completions endpoint supports tools in the OpenAI-compatible `tools` array:

- **Web search** (`web_search`): built-in web search tool. Returns sources in the response.
- **Retrieval** (`retrieval`): RAG-style retrieval from provided documents.
- **Function calling**: custom function definitions with JSON Schema parameters.

See `open.bigmodel.cn/dev/api/normal-model/glm-4` for the full tool-use request format.

## Not Supported

- **OpenAI Responses API** (`/v1/responses`): returns 404 on both general and coding
  endpoints. Codex CLI (which requires Responses API via `wire_api = "responses"`)
  cannot be routed through Z.ai.
- **No OpenAPI/Swagger manifest**: probed `/openapi.json`, `/swagger.json`,
  `/.well-known/openapi.json`, `/docs`, `/api-docs` — all 404.

## Non-functional Gateway Adapters

Z.ai's gateway registers routes for many provider prefixes (`/api/v1/`, `/api/v2/`,
`/api/v3/`, `/api/openai/`, `/api/bedrock/`, `/api/azure/`, `/api/gemini/`,
`/api/google/`, `/api/vertex/`) but they all return HTTP 200 with error JSON
`{"code":500,"msg":"404 NOT_FOUND"}`. Only the three functional adapters above
(`paas`, `coding/paas`, `anthropic`) actually serve GLM models.

## Available Models

As of 2026-05-08:

| Model ID      | Display Name |
| ------------- | ------------ |
| `glm-4.5`     | GLM-4.5      |
| `glm-4.5-air` | GLM-4.5-Air  |
| `glm-4.6`     | GLM-4.6      |
| `glm-4.7`     | GLM-4.7      |
| `glm-5`       | GLM-5        |
| `glm-5-turbo` | GLM-5-Turbo  |
| `glm-5.1`     | GLM-5.1      |

## Prompt Caching

Automatic (no request parameter); reported in `usage.prompt_tokens_details.cached_tokens`.

z.ai caches token **prefixes within a single message**, not just whole identical messages or
message-boundary prefixes. Measured 2026-06 (`glm-4.7`, coding endpoint): a ~12.5k-token prefix
shared between two requests that differ only in the trailing suffix is cached — `cached_tokens` is
`0` on a cold call, then `12544` on a follow-up whose first message shares that prefix but ends
differently. So a growing common prefix (e.g. an accumulating history rebuilt into the first user
message) is reused across calls without needing to split it into separate messages.

## Rate Limits and Plan Tiers

The coding-plan API key reaches **both** the general endpoint (where it can call the free `*-flash`
models at $0) and the coding endpoint.

- **Free general-endpoint models** (`glm-4.7-flash`, `glm-4.5-flash`) are aggressively shared-rate-
  limited: frequent `429` with `code 1302` ("Rate limit reached for requests") and `1305` ("service
  may be temporarily overloaded"). Keep concurrency low (≈2) and use exponential backoff.
- **Paid coding-endpoint models** (`glm-4.7`, `glm-4.6`) have dedicated, much higher rate limits —
  fast (~9 s for a small structured-JSON completion) and reliable — and draw the weekly token quota.
- Transient `503` with body `{"error":"DNS resolution failure"}` also appears on the coding endpoint;
  retry it as a 5xx.

## Request Parameter Notes

- `thinking: {"type": "disabled"}` disables reasoning (reasoning_tokens → 0). Recommended for
  structured-output tasks that don't need chain-of-thought (e.g. JSON generation).
- `response_format: {"type": "json_object"}` works on the paid coding models and on `glm-4.7-flash`,
  but free `glm-4.5-flash` returns `400` (`code 1210`, "Invalid API parameter") when `thinking` /
  `response_format` are present. **Probe a candidate model with the real generation params** before
  relying on it, rather than a bare smoke call.
- Some free models reject `temperature > 1.0` with `400`; `temperature: 1.0` is accepted across the
  models tested.

## Quota API (`/api/monitor/usage/quota/limit`)

Returns `data.limits[]`. The token limits (`type: "TOKENS_LIMIT"`, `unit: 3` = 5 h window,
`unit: 6` = 7 d window) expose only an integer `percentage` — **no raw used/total token counts**, so
sub-1% burn is invisible. There is also a `TIME_LIMIT` (`unit: 5`) tool/request quota carrying
`usage` / `currentValue` / `remaining` and per-model `usageDetails`. `data.level` reports the plan
tier (e.g. `"max"`). Track fine-grained token burn from your own usage totals, not this endpoint.

## Integration Notes

- **Claude Code** (`z-claude` alias): works via the Anthropic-compatible endpoint.
  Sets `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN=$ZAI_API_KEY`,
  `ANTHROPIC_MODEL=glm-5.1`.

- **Codex CLI**: not compatible — requires OpenAI Responses API which Z.ai does not expose.
  `wire_api = "chat"` (Chat Completions mode) was deprecated in Codex ~2026-04-21 and is no
  longer supported.

- **Canonical docs**: `open.bigmodel.cn` is Zhipu's developer platform with full API
  reference. The API at `api.z.ai` and `open.bigmodel.cn/api/paas/v4` are the same
  backend; `z.ai` is the international-facing domain.
