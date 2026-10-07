# API Research Notes

## Claude Code

Endpoint: `GET https://api.anthropic.com/api/oauth/usage`

Headers:

- `Authorization: Bearer <token>`
- `anthropic-beta: oauth-2025-04-20`

Token source: `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`

Response shape (relevant fields):

```json
{
  "five_hour": { "utilization": 45.0, "resets_at": "2025-05-01T18:00:00Z" },
  "seven_day": { "utilization": 12.0, "resets_at": "2025-05-07T00:00:00Z" }
}
```

`utilization` is a percentage consumed, 0–100, sent as a JSON number; observed values are whole numbers (<../README.md>
§ Gauge resolution). `resets_at` is an ISO 8601 UTC timestamp. These fields are all optional; check for null before
using. Per-model buckets (`seven_day_opus`, `seven_day_sonnet`) were absent from every body in the last 30 days.

Reference Python impl: `devinfra/claude/claude_api/usage.py` and `credentials.py`.

## OpenAI Codex

Source: <https://github.com/openai/codex>

Endpoint: `GET https://chatgpt.com/backend-api/wham/usage`

Headers:

- `Authorization: Bearer <token>`
- `ChatGPT-Account-Id: <account_id>`

Token source: `~/.codex/auth.json` → `tokens.access_token` and `tokens.account_id` (file-based; the Secret Service is
not available from the CLI).

Reference Python impl: `aiquota/providers/codex.py`.

Response shape (relevant fields):

```json
{
  "plan_type": "pro",
  "rate_limit": {
    "primary_window": {
      "used_percent": 6,
      "limit_window_seconds": 604800,
      "reset_after_seconds": 592777,
      "reset_at": 1784495031
    },
    "secondary_window": null
  }
}
```

`used_percent` is an integer 0–100. `reset_after_seconds` is seconds until reset; `reset_at` is a Unix epoch timestamp
(backup). Both windows are optional. From 2026-08-28 to 2026-09-30 the account's `rate_limit` carried only a weekly
window.

`primary_window` and `secondary_window` are transport slots, not duration semantics. Classify them using
`limit_window_seconds`; either slot may contain the weekly window, and either may be absent.
