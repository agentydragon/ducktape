# Plaid Spend

`//finance/plaid/spend:server_image` serves a backend-computed statement-cycle view to the desktop
app and a browser page at `/settings` for shared account configuration.

## Contract

- `GET /api/v1/view` returns the caller's enabled card aggregates. Money values are integer minor
  currency units; no transactions or Plaid credentials are returned.
- `GET /api/v1/events` sends an immediate `view` SSE event, then recomputes and sends a complete
  view after `plaid_spend_changed` or `plaid_spend_config_changed` notifications. Reconnects
  compute a new initial view; idle streams send comments as heartbeats.
- `GET /api/v1/config` and `PUT /api/v1/config` read or replace the caller's `{ "cards": [...] }`
  configuration. Each item has `account_id`, `label`, optional `limit_minor_units`, optional
  `alert_threshold_percent`, and `enabled`. Writes accept only active Plaid credit accounts.
- `/settings` uses a separate Authentik browser session and stores the same per-user configuration as the native API.

The view shape is:

```json
{
  "generated_at": "2026-09-30T12:00:00Z",
  "cards": [{
    "account_id": "plaid-account-id",
    "label": "Everyday card",
    "account_name": "Plaid account name",
    "institution_name": "Bank name",
    "mask": "1234",
    "currency": "USD",
    "cycle_start": "2026-09-01",
    "spend_minor_units": 34218,
    "posted_minor_units": 30000,
    "pending_minor_units": 4218,
    "limit_minor_units": 100000,
    "alert_threshold_percent": 80,
    "spend_percent": 34.218,
    "alert_state": "normal",
    "last_synced_at": "2026-09-30T11:58:00Z",
    "statement_available": true
  }]
}
```

Null cycle totals indicate no statement date; `institution_name` and `mask` may be null. Minor units
follow the currency's ISO precision (for example, USD cents and JPY whole yen).

The statement cycle starts the day after the latest credit-liability snapshot's
`last_statement_issue_date`. A missing date leaves cycle and spend values null and marks the card
unavailable; the service does not substitute calendar-month bounds. Posted and pending amounts are
summed separately, pending rows superseded by a posted transaction are dropped, removed rows and
credit-card payment transactions are excluded, and transactions with an explicit different currency
are not included. Refunds retain Plaid's negative sign. Spend percentage uses the configured limit;
the alert becomes `warning` at its threshold and `exceeded` at 100 percent. Card views identify both
the Plaid account name and its institution when available.

## Runtime configuration

All service settings use the `PLAID_SPEND_` prefix except `DATABASE_URL`:

- `DATABASE_URL`
- `PLAID_SPEND_API_OIDC_ISSUER`, `PLAID_SPEND_API_OIDC_CLIENT_ID`,
  `PLAID_SPEND_API_OIDC_DISCOVERED_ISSUER`, `PLAID_SPEND_API_OIDC_JWKS_URI`, and optional
  comma-separated `PLAID_SPEND_API_OIDC_SIGNING_ALGORITHMS` (default `RS256`)
- `PLAID_SPEND_BROWSER_OIDC_ISSUER`, `PLAID_SPEND_BROWSER_OIDC_CLIENT_ID`,
  `PLAID_SPEND_BROWSER_OIDC_CLIENT_SECRET`, `PLAID_SPEND_BROWSER_OIDC_SESSION_SECRET`, and
  optional `PLAID_SPEND_BROWSER_OIDC_SESSION_SECONDS`
- `PLAID_SPEND_PUBLIC_BASE_URL`; optional `PLAID_SPEND_HOST` and `PLAID_SPEND_PORT`

The API resolver verifies Authentik RS256 access tokens against pinned issuer/JWKS metadata and
requires the configured audience and authorized party. Browser and API identities share their
Authentik user UUID subject; configuration is keyed by `subject` alone. The database role reads the
existing `public` Plaid mirror and creates/writes only `plaid_spend.card_configs`. Infra owns
creating `plaid_spend` and grants the role `USAGE, CREATE` on that schema; the app waits and retries
table creation if the schema is not ready when Flux starts the Deployment. It does not need
database-level CREATE and does not write to the public mirror. Startup runs this idempotent DDL:

```sql
CREATE TABLE IF NOT EXISTS plaid_spend.card_configs (
    subject TEXT NOT NULL CHECK (length(btrim(subject)) > 0),
    account_id TEXT NOT NULL CHECK (length(btrim(account_id)) > 0),
    label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 80),
    limit_minor_units BIGINT CHECK (limit_minor_units IS NULL OR limit_minor_units > 0),
    alert_threshold_percent INTEGER CHECK (
        alert_threshold_percent IS NULL OR alert_threshold_percent BETWEEN 1 AND 100
    ),
    enabled BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, account_id)
)
```

The API emits `plaid_spend_config_changed` after config replacement and refreshes local SSE streams
after commit; the notification integration emits `plaid_spend_changed` when mirrored records
change.
