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
