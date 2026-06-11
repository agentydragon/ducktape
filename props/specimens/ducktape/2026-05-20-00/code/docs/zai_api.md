# z.ai API — Reverse-Engineered Quota Endpoints

z.ai (ZhipuAI's developer platform) does not document quota/usage APIs in its
public developer docs, but they exist and work with the standard API key.

## Authentication

All requests to `https://api.z.ai/api/` accept:

| Method      | Header          | Value          |
| ----------- | --------------- | -------------- |
| API key     | `Authorization` | `Bearer <key>` |
| Session JWT | `Authorization` | `Bearer <jwt>` |

The **API key** (`ZAI_API_KEY` env var, format `<hex>.<base64>`) works for most
quota endpoints. The session JWT (stored in cookie
`z-ai-open-platform-token-production`) is needed for a few account-management
endpoints.

## Quota / Usage Endpoints

### `GET /api/monitor/usage/quota/limit` ✅ Works with API key

Returns current quota usage across all limit types. No query parameters required.

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

**Field meanings** (decoded from the z.ai front-end JS bundle):

| `type`         | `unit` | Window title                                               | `unit_text`   |
| -------------- | ------ | ---------------------------------------------------------- | ------------- |
| `TOKENS_LIMIT` | `3`    | "5 Hours Quota" (peak hours: 14:00–18:00 UTC+8 daily, ~5h) | Tokens        |
| `TOKENS_LIMIT` | `6`    | "Weekly Quota" (7 days)                                    | Tokens        |
| `TIME_LIMIT`   | `5`    | "Total Monthly Web Search / Reader / Zread"                | Times (count) |

- `percentage` — percent of quota consumed (0–100)
- `currentValue` — amount used (for `TIME_LIMIT` only)
- `usage` — total limit (for `TIME_LIMIT` only)
- `remaining` — remaining amount (for `TIME_LIMIT` only)
- `nextResetTime` — Unix timestamp in milliseconds when this window resets
- `usageDetails` — per-tool breakdown (only present on `TIME_LIMIT` items)
- `level` — subscription tier (`"max"` for GLM Coding Max plan)

### `GET /api/monitor/usage/model-usage` ✅ Works with API key

Returns daily model call counts and token usage over a date range.

**Required params** (use `yyyy-MM-dd HH:mm:ss` format — ISO 8601 rejected):

```
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

### `GET /api/monitor/usage/model-performance-day` ✅ Works with API key

Returns daily decode speed (tokens/second) per tier.

**Params**: same date range format as `model-usage`.

```json
{
  "data": {
    "x_time": ["2026-05-01", ...],
    "liteDecodeSpeed": [70.18, 74.81, ...],
    "proMaxDecodeSpeed": [94.88, 94.05, ...]
  }
}
```

### `GET /api/monitor/usage/tool-usage` ⚠ Exists, needs params

Returns empty body without date params; likely requires same date range format.

## Subscription Endpoints

### `GET /api/biz/subscription/list` ✅ Works with API key

Returns current subscription plan details.

```json
{
  "data": [
    {
      "productName": "GLM Coding Max",
      "status": "VALID",
      "valid": "2026-08-07 04:00:24-2026-11-07 04:00:24",
      "billingCycle": "quarterly",
      "nextRenewTime": "2026-08-07"
    }
  ]
}
```

### `GET /api/biz/customer/speed/config/queryCustomerRpm?customerId={id}` ✅ Works with API key

Returns per-model RPM (requests per minute) limits. `customerId` is the numeric
customer ID from `getCustomerInfo`. Works with API key as auth.

### `GET /api/biz/customer/getCustomerInfo` ✅ Works with session JWT only

Returns account info including `customerNumber`, organizations, projects.
API key returns `"APIKey not allow access"`.

## Billing Endpoints

These work with session JWT:

| Endpoint                                                | Description                                          |
| ------------------------------------------------------- | ---------------------------------------------------- |
| `GET /api/biz/pay/check-pending-orders`                 | `{ "hasPendingOrders": false }`                      |
| `GET /api/finance/orderDetail/orderList`                | Order history (paginated with `pageNum`, `pageSize`) |
| `GET /api/biz/recharge/user-recharge-list`              | Recharge/top-up history                              |
| `GET /api/platform-charge-zai/alert/query/{customerId}` | Balance alert settings                               |

## Endpoints That Exist but Return 500

These routes return HTTP 200 with `code: 500` (application error), suggesting
they are stubbed or require parameters not yet determined:

- `GET /api/biz/customer/usage`
- `GET /api/biz/customer/plan`
- `GET /api/biz/customer/limit`
- `GET /api/biz/customer/rateLimit`
- `GET /api/biz/account/usage`
- `GET /api/biz/account/quota`
- `GET /api/biz/account/limit`
- `GET /api/biz/customer/tokens`
- `GET /api/biz/customer/balance`

## Notes

- **Reset times in error messages are likely CST (UTC+8)**. When the API returns
  `"Your limit will reset at 2026-05-15 17:29:01"`, the timestamp is probably
  China Standard Time (inferred from z.ai being Zhipu/China-based and the math
  fitting a 5-hour window) — subtract 8 hours to convert to UTC. Not officially
  documented.
- `usageBoard` is a feature gated behind a label whitelist
  (`GET /api/biz/label/whitelist/check?labelValue=usageBoard`). When enabled,
  it likely exposes a richer usage dashboard. Currently not enabled for this
  account (`data: true` but pages for it don't exist in the current bundle).
- The JWT has no `exp` claim, so it may be long-lived. However, obtaining a
  fresh one requires browser session auth.
- The API base URL is `https://api.z.ai/api` (different subdomain from the
  web app at `https://z.ai`).
