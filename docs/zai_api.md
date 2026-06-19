# Z.ai API

Z.ai (international frontend for Zhipu/智谱) exposes GLM models through multiple
API-compatible endpoints. Canonical docs at <https://open.bigmodel.cn/dev/api>.
Auth uses a single API key (`ZAI_API_KEY`) across all endpoints.

The standard API key also works for several reverse-engineered account and quota
endpoints under `https://api.z.ai/api/`. A few account-management endpoints need
the browser session JWT from the `z-ai-open-platform-token-production` cookie
instead.

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

Empirical notes for the coding endpoint (`glm-4.6`, measured 2026-06-05):

- Function tools work with ordinary OpenAI chat-completions `tools`. The response uses
  `finish_reason: "tool_calls"` and returns `message.tool_calls[]` with
  `function.name` and JSON-string `function.arguments`.
- `tool_choice: "auto"` works, and omitting `tool_choice` also works.
- OpenAI strict tool metadata is accepted by the API: `function.strict: true`,
  `required`, `enum`, and `additionalProperties: false` were accepted in a
  function tool schema. Do not read this as full OpenAI-equivalent enforcement:
  in a live props critic run on 2026-06-06 (`glm-4.6` via LiteLLM chat
  completions), the model still produced schema-invalid tool calls, including a
  `cmd` field stringifying the whole argv list instead of returning `list[str]`,
  and a malformed tool-call name resembling `python3</arg_value>`. Props' local
  tool validation rejected those calls, but z.ai returned them despite the
  strict metadata.
- `glm-4.6` has a reproducible issue with required nullable fields under strict
  tool schemas. Direct z.ai and cluster LiteLLM tests both returned `"null"` as
  a string for required nullable string/array fields, and returned a stringified
  JSON array for a required nullable array. Required non-null string/array
  fields worked, and non-null sentinel shapes worked. Prefer non-null sentinel
  or mode-object schemas over `anyOf: [T, null]` for z.ai tool inputs.
- The `anyOf` weakness extends to **object-typed union parameters**. A tool
  parameter whose schema is an `anyOf`/`oneOf` of object variants (e.g. a
  Pydantic discriminated union) comes back as a **JSON-encoded string**, not a
  nested object: the top-level `arguments` still parses as valid JSON, but the
  union field's value is a string, so server-side Pydantic rejects it with
  `model_attributes_type` ("Input should be a valid dictionary or object to
  extract fields from"). Measured 2026-06-07 against the coding endpoint and
  reproduced in a live props `critic_dev_optimize` run (`a4cb7710`): every
  `start_critic` call stringified its `example` union and failed validation,
  exhausting the agent's budget on retries.
  - The trigger is the **union combinator** (`anyOf` _and_ `oneOf`, 3/3
    stringified each) — not `strict`, not `$ref`/`$defs`, and not the
    discriminator (inlining or dropping them doesn't help).
  - **Working shapes** (3/3 proper objects): a single concrete object schema
    with defined `properties` — `const` or multi-value `enum` `kind`,
    `additionalProperties` either `true` or `false` — and fully-flat top-level
    scalar params (no nesting).
  - **Root cause** (not z.ai-API-specific — a GLM model-level bug, reproduced on
    both the z.ai API and OpenCode Zen): GLM emits tool calls in an **XML**
    format (`<parameter name="x">…</parameter>`), and the XML→arguments parser
    stringifies a tag's content unless the schema declares a single concrete
    type. A plain `object` schema is JSON-parsed back into an object; an
    `anyOf`/`oneOf`/`allOf` union is type-ambiguous, so the parser leaves the
    raw JSON string. See
    [zai-org/GLM-4.7 discussion #18](https://huggingface.co/zai-org/GLM-4.7/discussions/18)
    ("Unable to produce correct `object` type tool call param", open/unresolved
    as of 2025-12). z.ai's own
    [function-calling docs](https://docs.z.ai/guides/capabilities/function-calling)
    are examples-only and say nothing about schema-feature support; the single
    explicit schema constraint they state is `tool_choice` "only supports auto".
  - **The bug is chat-completions-shape-specific.** The same `anyOf`/`oneOf`
    union, sent to z.ai's **Anthropic Messages** endpoint
    (`/api/anthropic/v1/messages`) with the schema as the tool `input_schema`,
    comes back as a proper nested object (3/3 for both `anyOf` and `oneOf`,
    measured 2026-06-07). The Anthropic wire format requires `tool_use.input` to
    be a JSON object, so z.ai's Anthropic adapter parses GLM's XML tool call back
    into a structured object instead of leaving the raw string. So routing
    glm-4.6 through the Anthropic shape avoids the bug without flattening any
    schema. Canaried live in `agent_core/test_zai_chat_adapter_live.py`
    (`test_zai_anthropic_shape_union_tool_param_returned_as_object_live`).
  - **Recipe**: either (a) route glm-4.6 through the Anthropic Messages shape and
    keep union schemas as-is, or (b) on chat-completions, represent a
    discriminated union as a single concrete object (carry the superset of
    fields, keep `kind` as an `enum`, enforce the per-`kind` required fields
    server-side) or as flat top-level params — never `anyOf`/`oneOf`. All working
    shapes are canaried live in `agent_core/test_zai_chat_adapter_live.py`.
- Forced named tool choice in the OpenAI object form is rejected:
  `{"type": "function", "function": {"name": "..."}}` returns `400` with
  `{"error":{"code":"1210","message":"Invalid API parameter, please check the documentation."}}`.
  If a caller needs z.ai to call a specific tool, use prompt instructions plus
  `tool_choice: "auto"` rather than the forced named object shape.
- System-only chat requests are rejected by the coding endpoint. A request with a
  single `{"role":"system", ...}` message plus function tools returned `400` /
  code `1214` with message `The messages parameter is illegal. Please check the
documentation.` The same request shape succeeds when the task is in a `user`
  message after the system message, returning `finish_reason: "tool_calls"`.

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

As of 2026-06-19:

| Model ID      | Display Name |
| ------------- | ------------ |
| `glm-4.5`     | GLM-4.5      |
| `glm-4.5-air` | GLM-4.5-Air  |
| `glm-4.6`     | GLM-4.6      |
| `glm-4.7`     | GLM-4.7      |
| `glm-5`       | GLM-5        |
| `glm-5-turbo` | GLM-5-Turbo  |
| `glm-5.1`     | GLM-5.1      |
| `glm-5.2`     | GLM-5.2      |

`glm-5.2` (released 2026-06-17) ships a 1M-token context window, up from the 200k
of the earlier GLM-4.x / 5.x models.

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
- Both `max_completion_tokens` and legacy `max_tokens` are accepted by the OpenAI-compatible
  chat-completions endpoint.
- `response_format: {"type": "json_object"}` works on the paid coding models and on `glm-4.7-flash`,
  but free `glm-4.5-flash` returns `400` (`code 1210`, "Invalid API parameter") when `thinking` /
  `response_format` are present. **Probe a candidate model with the real generation params** before
  relying on it, rather than a bare smoke call.
- Some free models reject `temperature > 1.0` with `400`; `temperature: 1.0` is accepted across the
  models tested.

## Account, Quota, and Usage APIs

z.ai does not document these quota/usage APIs in the public developer docs, but
they exist and work with the standard API key except where noted.

### Authentication

All requests to `https://api.z.ai/api/` accept:

| Method      | Header          | Value          |
| ----------- | --------------- | -------------- |
| API key     | `Authorization` | `Bearer <key>` |
| Session JWT | `Authorization` | `Bearer <jwt>` |

The API key (`ZAI_API_KEY`, format `<hex>.<base64>`) works for most quota
endpoints. The session JWT is needed for a few account-management and billing
endpoints.

### `GET /api/monitor/usage/quota/limit`

Works with API key. Returns current quota usage across all limit types. No query
parameters required.

```json
{
  "code": 200,
  "data": {
    "limits": [
      {
        "type": "TIME_LIMIT",
        "unit": 5,
        "number": 1,
        "usage": 4000,
        "currentValue": 82,
        "remaining": 3918,
        "percentage": 2,
        "nextResetTime": 1780776023997,
        "usageDetails": [
          { "modelCode": "search-prime", "usage": 0 },
          { "modelCode": "web-reader", "usage": 82 },
          { "modelCode": "zread", "usage": 0 }
        ]
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 3,
        "number": 5,
        "percentage": 0
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 6,
        "number": 1,
        "percentage": 68,
        "nextResetTime": 1778702423997
      }
    ],
    "level": "max"
  },
  "success": true
}
```

Field meanings decoded from the z.ai front-end JS bundle:

| `type`         | `unit` | Window title                                               | `unit_text`   |
| -------------- | ------ | ---------------------------------------------------------- | ------------- |
| `TOKENS_LIMIT` | `3`    | "5 Hours Quota" (peak hours: 14:00-18:00 UTC+8 daily, ~5h) | Tokens        |
| `TOKENS_LIMIT` | `6`    | "Weekly Quota" (7 days)                                    | Tokens        |
| `TIME_LIMIT`   | `5`    | "Total Monthly Web Search / Reader / Zread"                | Times (count) |

- `percentage`: percent of quota consumed (0-100).
- `currentValue`: amount used, for `TIME_LIMIT` only.
- `usage`: total limit, for `TIME_LIMIT` only.
- `remaining`: remaining amount, for `TIME_LIMIT` only.
- `nextResetTime`: Unix timestamp in milliseconds when this window resets.
- `usageDetails`: per-tool breakdown, only present on `TIME_LIMIT` items.
- `level`: subscription tier, e.g. `"max"` for GLM Coding Max plan.

The token limits expose only integer `percentage` values, not raw used/total
token counts, so sub-1% burn is invisible. Track fine-grained token burn from
your own usage totals, not this endpoint.

### `GET /api/monitor/usage/model-usage`

Works with API key. Returns daily model call counts and token usage over a date
range.

Required params use `yyyy-MM-dd HH:mm:ss` format. ISO 8601 is rejected.

```text
?startTime=2026-05-01 00:00:00&endTime=2026-05-10 23:59:59
```

```json
{
  "code": 200,
  "data": {
    "x_time": ["2026-05-07", "2026-05-08", "2026-05-09", "2026-05-10"],
    "modelCallCount": [290, 2813, 916, 1746],
    "tokensUsage": [29516565, 299752372, 106248228, 229514568],
    "totalUsage": {
      "totalModelCallCount": 5765,
      "totalTokensUsage": 665031733,
      "modelSummaryList": [{ "modelName": "GLM-5.1", "totalTokens": 333868066, "sortOrder": 1 }]
    }
  }
}
```

### `GET /api/monitor/usage/model-performance-day`

Works with API key. Returns daily decode speed (tokens/second) per tier. Params
use the same date range format as `model-usage`.

```json
{
  "data": {
    "x_time": ["2026-05-01"],
    "liteDecodeSpeed": [70.18],
    "proMaxDecodeSpeed": [94.88]
  }
}
```

### Other Usage and Subscription Endpoints

- `GET /api/monitor/usage/tool-usage`: exists, but returns an empty body without
  date params; likely requires the same date range format.
- `GET /api/biz/subscription/list`: works with API key and returns current
  subscription details (`productName`, `status`, `valid`, `billingCycle`,
  `nextRenewTime`).
- `GET /api/biz/customer/speed/config/queryCustomerRpm?customerId={id}`: works
  with API key and returns per-model RPM limits. `customerId` comes from
  `getCustomerInfo`.
- `GET /api/biz/customer/getCustomerInfo`: requires session JWT; API key returns
  `"APIKey not allow access"`.

Session-JWT billing endpoints:

| Endpoint                                                | Description                                          |
| ------------------------------------------------------- | ---------------------------------------------------- |
| `GET /api/biz/pay/check-pending-orders`                 | `{ "hasPendingOrders": false }`                      |
| `GET /api/finance/orderDetail/orderList`                | Order history (paginated with `pageNum`, `pageSize`) |
| `GET /api/biz/recharge/user-recharge-list`              | Recharge/top-up history                              |
| `GET /api/platform-charge-zai/alert/query/{customerId}` | Balance alert settings                               |

These routes return HTTP 200 with `code: 500`, suggesting they are stubbed or
require parameters not yet determined:

- `GET /api/biz/customer/usage`
- `GET /api/biz/customer/plan`
- `GET /api/biz/customer/limit`
- `GET /api/biz/customer/rateLimit`
- `GET /api/biz/account/usage`
- `GET /api/biz/account/quota`
- `GET /api/biz/account/limit`
- `GET /api/biz/customer/tokens`
- `GET /api/biz/customer/balance`

Notes:

- Reset times in error messages are likely CST (UTC+8). When the API returns
  `"Your limit will reset at 2026-05-15 17:29:01"`, the timestamp is probably
  China Standard Time, inferred from z.ai being Zhipu/China-based and the math
  fitting a 5-hour window.
- `usageBoard` is gated behind
  `GET /api/biz/label/whitelist/check?labelValue=usageBoard`. When enabled, it
  likely exposes a richer usage dashboard. It was not enabled for this account
  when checked (`data: true` but pages for it did not exist in the current
  bundle).
- The JWT has no `exp` claim, so it may be long-lived. Obtaining a fresh one
  still requires browser session auth.

## Integration Notes

- **Claude Code** (`z-claude` alias): works via the Anthropic-compatible endpoint.
  Sets `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN=$ZAI_API_KEY`,
  `ANTHROPIC_MODEL=glm-5.2`.

- **Codex CLI**: not compatible — requires OpenAI Responses API which Z.ai does not expose.
  `wire_api = "chat"` (Chat Completions mode) was deprecated in Codex ~2026-04-21 and is no
  longer supported.

- **Canonical docs**: `open.bigmodel.cn` is Zhipu's developer platform with full API
  reference. The API at `api.z.ai` and `open.bigmodel.cn/api/paas/v4` are the same
  backend; `z.ai` is the international-facing domain.
