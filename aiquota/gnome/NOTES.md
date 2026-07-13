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
  "five_hour": { "utilization": 45.2, "resets_at": "2025-05-01T18:00:00Z" },
  "seven_day": { "utilization": 12.7, "resets_at": "2025-05-07T00:00:00Z" },
  "seven_day_opus": { "utilization": 0.0, "resets_at": "..." },
  "seven_day_sonnet": { "utilization": 8.1, "resets_at": "..." }
}
```

`utilization` is a float in the range 0–100 (percentage consumed). `resets_at` is an ISO
8601 UTC timestamp. These fields are all optional; check for null before using.

Reference Python impl: `devinfra/claude/claude_api/usage.py` and `credentials.py`.

## OpenAI Codex

Source: <https://github.com/openai/codex>

Endpoint: `GET https://chatgpt.com/backend-api/api/codex/usage`

Headers:

- `Authorization: Bearer <token>`

Token storage: OS keyring, service name `"Codex Auth"` (from
`codex-rs/login/src/auth/storage.rs`). On Linux, this is the Secret Service
(GNOME Keyring / KWallet). Accessible via `gi://Secret` in GNOME Shell
extensions — see `_readCodexToken()` in `extension.js`.

Response shape (relevant fields):

```json
{
  "plan_type": "Pro",
  "rate_limit": {
    "primary_window": {
      "used_percent": 45,
      "limit_window_seconds": 3600,
      "reset_after_seconds": 1234,
      "reset_at": 1715018400
    },
    "secondary_window": {
      "used_percent": 12,
      "limit_window_seconds": 86400,
      "reset_after_seconds": 45000,
      "reset_at": 1715068800
    }
  }
}
```

`used_percent` is an integer 0–100. `reset_after_seconds` is seconds until reset;
`reset_at` is a Unix epoch timestamp (backup). Both windows are optional.

`primary_window` and `secondary_window` are transport slots, not duration
semantics. Classify them using `limit_window_seconds`; either slot may contain
the weekly window, and either may be absent.
